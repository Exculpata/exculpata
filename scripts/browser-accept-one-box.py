#!/usr/bin/env python3
"""Exercise the optional shared Home entry point with temporary synthetic records.

The deterministic answer client checks routing and cited-answer presentation;
it does not measure model quality. Every action uses the existing product UI.
"""
from __future__ import annotations

import argparse
import io
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import threading
from urllib.parse import parse_qs, urlsplit

import uvicorn
from selenium import webdriver
from selenium.webdriver import ActionChains
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from synthetic_browser_environment import isolate_environment
from case_intelligence.ask_router import classify
from case_intelligence.workbench import create_workbench_app

ACTOR = "development-taylor-morgan"
SOURCE_NAME = "Synthetic arrival ledger.txt"
SOURCE_TEXT = "The amber bicycle arrived at noon."
EXACT = '"amber bicycle"'
QUESTION = "When did the amber bicycle arrive?"
UNSUPPORTED_QUESTION = "Why did the amber bicycle arrive at noon?"
EVERY_SOURCE = "Find every record about the amber bicycle."


class SyntheticAnswerGenerator:
    available = True

    def __init__(self):
        self.calls = 0

    def generate(self, *, question, evidence, **kwargs):
        self.calls += 1
        if question == UNSUPPORTED_QUESTION:
            return {"answerable": False, "claims": [], "limitation": None,
                    "missing_information": "The synthetic arrival record gives no reason."}
        return {"answerable": bool(evidence), "claims": [
            {"text": item.excerpt, "evidence_ids": [item.evidence_id]}
            for item in evidence[:1]], "limitation": None, "missing_information": ""}


def prepare_output(output: Path) -> None:
    if output.is_symlink() or (output.exists() and (not output.is_dir() or any(output.iterdir()))):
        raise ValueError("Choose a fresh or empty output directory; existing results were preserved.")
    output.mkdir(parents=True, exist_ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chrome-binary", type=Path, required=True)
    parser.add_argument("--chromedriver", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    prepare_output(args.output)
    isolate_environment()
    os.environ["CASE_INTELLIGENCE_ONE_BOX"] = "1"
    os.environ["CASE_INTELLIGENCE_DEEPER_INVESTIGATION"] = "1"
    report = {"synthetic_only": True, "passed": False, "checks": []}
    checks = report["checks"]
    with tempfile.TemporaryDirectory(prefix="recordbench-one-box-") as temporary:
        generator = SyntheticAnswerGenerator()
        app = create_workbench_app(Path(temporary) / "runtime", generator=generator,
            auth_mode="test", learned_retrieval=False, background_ingestion=False)
        bench = app.state.workbench
        matter = bench.create_matter("Synthetic shared entry", "Invented browser acceptance records", ACTOR)
        empty = bench.create_matter("Synthetic empty shared entry", "Invented empty matter", ACTOR)
        document, _ = bench.source_store(matter).store_stream(SOURCE_NAME, "text/plain",
            io.BytesIO(SOURCE_TEXT.encode()))
        bench._sync_source_catalog(matter, [document])
        prefix = f"/matters/{matter.slug}"
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
        driver = None
        try:
            driver = webdriver.Chrome(service=Service(str(args.chromedriver)), options=options)
            driver.set_page_load_timeout(30)
            wait = WebDriverWait(driver, 30)
            wait.until(lambda _: server.started)

            def find(selector):
                return driver.find_element(By.CSS_SELECTOR, selector)

            def js(script, *values):
                return driver.execute_script(script, *values)

            def click(element):
                js("arguments[0].scrollIntoView({block:'center',behavior:'instant'})", element)
                element.click()

            def native_focus_visible(element):
                previous = None

                def settled_and_visible(_):
                    nonlocal previous
                    measurement = js("""const e=arguments[0],r=e.getBoundingClientRect();
                        const top=document.querySelector('.topbar')?.getBoundingClientRect().bottom || 0;
                        const dock=document.querySelector('[data-assistant-expand]');
                        const d=dock?.getBoundingClientRect();
                        let bottom=d && dock.getClientRects().length && d.width>innerWidth/2
                            && d.bottom>=innerHeight-2 ? d.top : innerHeight;
                        const composer=e.closest('.deeper-investigation')
                            && e.closest('.workspace-main')?.querySelector('.composer-wrap');
                        const c=composer?.getBoundingClientRect();
                        const s=getComputedStyle(e);
                        const outline=c && s.outlineStyle!=='none'
                            ? Math.max(0,(parseFloat(s.outlineWidth)||0)+(parseFloat(s.outlineOffset)||0)) : 0;
                        if(c && composer.getClientRects().length) bottom=Math.min(bottom,c.top);
                        return {control:e.id || e.dataset.oneBoxKind || e.tagName,
                            focused:document.activeElement===e,focus_visible:e.matches(':focus-visible'),
                            top:r.top,bottom:r.bottom,left:r.left,right:r.right,
                            focus_bottom:r.bottom+outline,composer_top:c?.top ?? null,
                            available_top:top,available_bottom:bottom,viewport_width:innerWidth,
                            scroll_y:scrollY,center_unobscured:e.contains(document.elementFromPoint(
                                r.left+r.width/2,r.top+r.height/2))};""", element)
                    report["last_focus_geometry"] = measurement
                    geometry = (measurement["top"], measurement["bottom"], measurement["scroll_y"])
                    settled = previous is not None and all(abs(now - before) <= .5
                        for now, before in zip(geometry, previous))
                    previous = geometry
                    return (settled and measurement["focused"] and measurement["focus_visible"]
                        and measurement["center_unobscured"]
                        and measurement["top"] >= measurement["available_top"] - 1
                        and measurement["bottom"] <= measurement["available_bottom"] + 1
                        and measurement["focus_bottom"] <= measurement["available_bottom"] + 1
                        and measurement["left"] >= -1
                        and measurement["right"] <= measurement["viewport_width"] + 1)

                wait.until(settled_and_visible,
                    "Native keyboard focus is outside the unobscured viewport or hidden behind another control")

            def keyboard_to(element):
                # Native Tab traversal, with no script focus or pointer action.
                for _ in range(100):
                    if driver.switch_to.active_element == element:
                        assert element.is_displayed() and element.is_enabled()
                        native_focus_visible(element)
                        return
                    ActionChains(driver).send_keys(Keys.TAB).perform()
                raise AssertionError("The requested control is absent from the native keyboard tab order")

            def home_submit(text, *, keyboard=False):
                driver.get(base + prefix + "/home")
                field = find("#one-box-question")
                assert field.is_enabled()
                if keyboard:
                    keyboard_to(field)
                field.send_keys(text)
                button = find("[data-one-box-form] button[type=submit]")
                if keyboard:
                    ActionChains(driver).send_keys(Keys.TAB).perform()
                    assert driver.switch_to.active_element == button
                    native_focus_visible(button)
                    button.send_keys(Keys.ENTER)
                else:
                    click(button)

            def destination(path, text):
                wait.until(lambda _: js("""return location.pathname === arguments[0]
                    && document.readyState === 'complete'
                    && document.querySelector('.one-box-route-reason')?.textContent.includes(arguments[1])""",
                    prefix + path, classify(text).reason))
                reason = find(".one-box-route-reason")
                assert reason.is_displayed() and classify(text).reason in reason.text

            def alternatives(current):
                summary = find("[data-one-box-switch] > summary")
                assert summary.text == "Not what you meant?"
                keyboard_to(summary)
                summary.send_keys(Keys.ENTER)
                controls = driver.find_elements(By.CSS_SELECTOR, "[data-one-box-kind]")
                assert {item.get_attribute("data-one-box-kind") for item in controls} == (
                    {"exact", "question", "every_source"} - {current})
                assert all(item.is_displayed() and item.is_enabled() for item in controls)
                return controls

            def no_overflow():
                wait.until(lambda _: js("return document.documentElement.scrollWidth <= innerWidth"))

            def no_review_started():
                assert bench.workspace.review_runs(matter.matter_id, ACTOR) == ()
                assert bench.workspace.review_criteria(matter.matter_id, ACTOR) == ()

            driver.get(base + prefix + "/home")
            field = find("#one-box-question")
            assert field.accessible_name == "Ask, search or find records"
            assert js("return Array.from(arguments[0].labels).some(e=>e.getClientRects().length)", field)
            form = find("[data-one-box-form]")
            assert urlsplit(form.get_attribute("action")).path == prefix + "/one-box"
            assert form.find_element(By.CSS_SELECTOR, 'input[name="csrf_token"]').get_attribute("value")
            launcher = find("[data-task-launcher]")
            quiet = {item.text: item.get_attribute("href") for item in launcher.find_elements(By.CSS_SELECTOR, "a")}
            assert urlsplit(quiet["Review records manually"]).path == prefix + "/setup"
            assert parse_qs(urlsplit(quiet["Review records manually"]).query)["view"] == ["list"]
            assert urlsplit(quiet["Investigate a topic"]).fragment == "matter-question"
            assert len(quiet) == 2 and "Ask a focused question" not in launcher.text
            checks.append("Home replaces task cards with one visibly labeled CSRF form and the two existing quiet links")

            home_submit(EXACT)
            destination("/exact-search", EXACT)
            assert parse_qs(urlsplit(driver.current_url).query)["q"] == [EXACT]
            wait.until(lambda _: "1 source found" in find("#find-results-heading").text)
            assert SOURCE_TEXT in find(".find-excerpt").text
            exact_url = driver.current_url
            checks.append("Quoted input reaches exact search with unchanged text, the classifier reason and the matching source")
            click(find(".find-open-source"))
            wait.until(lambda _: "/sources/" in urlsplit(driver.current_url).path)
            assert SOURCE_TEXT in find("main").text
            checks.append("Exact search opens the original synthetic source")

            driver.get(exact_url)
            controls = alternatives("exact")
            checks.append("The route switch exposes exactly the other two choices through a native keyboard disclosure")
            choose = next(item for item in controls if item.get_attribute("data-one-box-kind") == "every_source")
            keyboard_to(choose)
            choose.send_keys(Keys.ENTER)
            wait.until(lambda _: urlsplit(driver.current_url).path == prefix + "/full-review")
            assert find("[data-criterion-instructions]").get_attribute("value") == EXACT
            assert find(".workflow-scope-card").is_displayed()
            assert find(".workflow-scope-card strong").text == "1"
            no_review_started()
            assert generator.calls == 0
            checks.append("Keyboard route switching preserves the original quoted text and visible scope without starting a review")

            home_submit(EVERY_SOURCE)
            destination("/full-review", EVERY_SOURCE)
            assert find("[data-criterion-instructions]").get_attribute("value") == EVERY_SOURCE
            assert find(".workflow-scope-card").is_displayed()
            alternatives("every_source")
            checks.append("Every-source input prefills the existing criterion form and retains the scope preview and both alternatives")
            no_review_started()
            assert generator.calls == 0
            checks.append("Both every-source arrivals leave criteria, review runs and generation untouched until explicit confirmation")

            driver.execute_cdp_cmd("Emulation.setDeviceMetricsOverride", {
                "width": 430, "height": 932, "deviceScaleFactor": 1, "mobile": False})
            driver.get(base + prefix + "/home")
            no_overflow()
            field = find("#one-box-question")
            keyboard_to(field)
            assert js("return innerWidth") == 430
            assert js("const r=arguments[0].getBoundingClientRect();return r.left>=0&&r.right<=innerWidth", field)
            driver.save_screenshot(str(args.output / "one-box-home-430.png"))
            checks.append("At 430 CSS pixels, native Tab keeps the labeled box fully above the Assistant dock with its center unobscured and no horizontal overflow")

            home_submit(QUESTION, keyboard=True)
            destination("", QUESTION)
            wait.until(lambda _: js("return document.querySelector('.answer-claims')?.textContent.includes(arguments[0])", SOURCE_TEXT))
            assert generator.calls == 1
            assert not driver.find_elements(By.CSS_SELECTOR, "[data-deeper-investigation]")
            checks.append("A supported generated answer has no deeper-investigation offer")
            conversations = bench.workspace.conversations(matter.matter_id)
            messages = tuple(message for conversation in conversations
                for message in bench.workspace.messages(matter.matter_id, conversation.conversation_id))
            assert [message.content for message in messages if message.role == "user"] == [QUESTION]
            checks.append("Native Tab and Enter submit a question through the existing cited-answer flow exactly once with unchanged text")
            no_overflow()
            driver.save_screenshot(str(args.output / "one-box-answer-430.png"))
            alternatives("question")
            checks.append("The 430-pixel cited-answer destination shows its classifier reason and offers both other routes without overflow")
            click(find(".citation-ref"))
            wait.until(lambda _: find(".support-pane").is_displayed())
            assert SOURCE_TEXT in find(".support-pane").text
            checks.append("The generated answer citation opens the original supporting passage")

            driver.get(base + f"/matters/{empty.slug}/home")
            assert not find("#one-box-question").is_enabled()
            assert not find("[data-one-box-form] button[type=submit]").is_enabled()
            described = find("#one-box-question").get_attribute("aria-describedby").split()
            assert any(driver.find_element(By.ID, identifier).is_displayed()
                and driver.find_element(By.ID, identifier).text.strip() for identifier in described)
            checks.append("An empty matter disables the box and submit control with a visible associated readiness hint")

            # Retain the real research admission path and make its queued result
            # deterministic. No research generation is needed to check consent.
            bench.research.close()
            bench.research = None
            home_submit(UNSUPPORTED_QUESTION, keyboard=True)
            destination("", UNSUPPORTED_QUESTION)
            wait.until(lambda _: js("return !!document.querySelector('[data-deeper-investigation]')"))
            assert generator.calls == 2
            # Completion reloads the one-box conversation. The offer may be
            # visible in the retiring document, so re-find the answer on stale
            # reads while still requiring the same unsupported-answer text.
            wait.until(EC.text_to_be_present_in_element(
                (By.CSS_SELECTOR, ".answer-not-supported"), "I could not find enough support"))
            assert bench.workspace.research_jobs(matter.matter_id, ACTOR) == ()
            unsupported_url = driver.current_url
            conversation_id = parse_qs(urlsplit(unsupported_url).query)["conversation"][0]
            messages = bench.workspace.messages(matter.matter_id, conversation_id)
            assert any(message.role == "user" and message.content == UNSUPPORTED_QUESTION for message in messages)
            assert any(message.role == "assistant" and message.payload.get("kind") == "not-supported" for message in messages)
            driver.refresh()
            wait.until(lambda _: js("return !!document.querySelector('[data-deeper-investigation]')"))
            offers = driver.find_elements(By.CSS_SELECTOR, "[data-deeper-investigation]")
            assert len(offers) == 1
            offer = offers[0]
            assert urlsplit(offer.get_attribute("action")).path == prefix + "/ask"
            assert offer.get_attribute("method").lower() == "post"
            assert offer.find_element(By.CSS_SELECTOR, 'input[name="csrf_token"]').get_attribute("value")
            assert offer.find_element(By.CSS_SELECTOR, 'input[name="question"]').get_attribute("value") == UNSUPPORTED_QUESTION
            assert offer.find_element(By.CSS_SELECTOR, 'input[name="conversation"]').get_attribute("value") == conversation_id
            assert offer.find_element(By.CSS_SELECTOR, '[name="review_task"]').get_attribute("value") == "research"
            assert bench.workspace.research_jobs(matter.matter_id, ACTOR) == ()
            assert generator.calls == 2
            checks.append("A generator-declined answer retains the same question in one CSRF-protected offer; displaying and refreshing it starts no investigation")
            button = offer.find_element(By.CSS_SELECTOR, 'button[type="submit"]')
            assert button.text == "Investigate this more deeply"
            keyboard_to(button)
            no_overflow()
            driver.save_screenshot(str(args.output / "deeper-investigation-offer-430.png"))
            button.click()  # One native activation; no automatic or retried submit.
            wait.until(lambda _: len(bench.workspace.research_jobs(matter.matter_id, ACTOR)) == 1)
            wait.until(lambda _: js("return !!document.querySelector('.conversation-research-progress')"))
            [job] = bench.workspace.research_jobs(matter.matter_id, ACTOR)
            assert job.question == UNSUPPORTED_QUESTION and job.conversation_id == conversation_id
            assert job.state == "queued" and job.actor_id == ACTOR
            assert job.idempotency_key.startswith("research-request-")
            assert UNSUPPORTED_QUESTION in find(".conversation-research-progress").text
            driver.refresh()
            wait.until(lambda _: js("return !!document.querySelector('.conversation-research-progress')"))
            assert len(bench.workspace.research_jobs(matter.matter_id, ACTOR)) == 1
            assert generator.calls == 2
            checks.append("At 430 pixels, one native offer click queues exactly one canonical research job with the unchanged question and conversation; refreshing never resubmits it")
            report["passed"] = True
            (args.output / "receipt.json").write_text(json.dumps(report, indent=2) + "\n")
            print(json.dumps(report))
        except Exception as error:
            report.update(passed=False, error={"type": type(error).__name__, "message": str(error)[:2000]})
            if driver is not None:
                try:
                    location = urlsplit(driver.current_url)
                    report["failure_location"] = location.path + ("?" + location.query if location.query else "")
                    driver.save_screenshot(str(args.output / "failure.png"))
                except Exception as capture_error:
                    report["screenshot_error"] = type(capture_error).__name__
            (args.output / "receipt.json").write_text(json.dumps(report, indent=2) + "\n")
            raise
        finally:
            if driver is not None:
                driver.quit()
            server.should_exit = True
            thread.join(10)
            listener.close()


if __name__ == "__main__":
    main()
