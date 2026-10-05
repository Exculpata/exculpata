#!/usr/bin/env python3
"""Check the assistant dock's Close control, page-aware suggestions and answer-passage actions with synthetic records."""
from __future__ import annotations

import argparse
import io
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading

import uvicorn
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from case_intelligence.generation import UnavailableGenerator
from case_intelligence.managed_storage import StoragePolicy
from case_intelligence.workbench import create_workbench_app

ACTOR = "development-taylor-morgan"
SOURCE_NAME = "Synthetic dock journey — North Annex receiving notes with a long recognizable filename.txt"
SOURCE_TEXT = "The blue crate arrived at the North Annex on 2026-03-05 at 09:15, signed for by Alex Example."


def prepare_output(output: Path) -> None:
    """Accept the runner's new empty directory, never overwrite older evidence."""
    if output.is_symlink() or (output.exists() and (not output.is_dir() or any(output.iterdir()))):
        raise ValueError("Choose a fresh or empty output directory; existing results were preserved.")
    output.mkdir(parents=True, exist_ok=True)


def failure_evidence(output: Path, driver, report: dict, error: Exception) -> None:
    report.update(passed=False, error={"type": type(error).__name__, "message": str(error)[:2000]})
    if driver is not None:
        try:
            driver.save_screenshot(str(output / "failure.png"))
        except Exception as capture_error:
            report["screenshot_error"] = type(capture_error).__name__
    (output / "receipt.json").write_text(json.dumps(report, indent=2) + "\n")


def seed(bench):
    matter = bench.create_matter("Synthetic dock review", "Invented records for dock acceptance", ACTOR)
    store = bench.source_store(matter)
    document, _ = store.store_stream(SOURCE_NAME, "text/plain", io.BytesIO(SOURCE_TEXT.encode()))
    return f"/matters/{matter.slug}", f"/matters/{matter.slug}/sources/{store.action_token(document)}"


def seed_answer(bench, prefix, document=None):
    """Add a synthetic verified answer whose one claim cites the journey source (or the one given)."""
    matter = bench.workspace.get_matter(prefix.rsplit("/", 1)[1], ACTOR)
    document = document or next(iter(bench.source_store(matter).documents.values()))
    bench._sync_source_catalog(matter, (document,))
    citation = bench._citation(matter, bench._candidate(matter, document, document.parsed_units()[0], 1))
    conversation = bench.workspace.get_conversation(matter.matter_id)
    bench.workspace.append_message(matter.matter_id, conversation.conversation_id, "assistant", "Synthetic answer",
        {"kind": "generated", "claims": [{"text": "Alex Example signed for the blue crate.",
                                          "citations": [bench._saved_answer_citation_payload(citation)]}]})


class SyntheticDrafts:
    """A deterministic drafting client for the journey; it drafts fixed questions from the passage it is given."""
    available = True

    def draft_questions(self, *, purpose, topic, evidence):
        first = evidence[0].evidence_id
        return {"questions": [
            {"text": "Who else was at the North Annex when the blue crate arrived on 2026-03-05?", "evidence_ids": [first]},
            {"text": "How did Alex Example confirm what was in the blue crate before signing?", "evidence_ids": [first]},
            {"text": "Why does the record say \"it was already open\"?", "evidence_ids": [first]},
            # Grounded only in the second answer's passage; the check drops it for the first.
            {"text": "Who saw Morgan Example sign the second log on 2026-04-01?", "evidence_ids": [first]},
        ]}


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--chrome-binary", type=Path, required=True)
    parser.add_argument("--chromedriver", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    prepare_output(args.output)
    from synthetic_browser_environment import isolate_environment
    isolate_environment()
    checks = []
    receipt = {"passed": False, "synthetic_only": True,
        "provenance": "temporary synthetic records; no model or live storage",
        "commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        "checks": checks,
        "unrun_checks": ["screen reader", "WebKit", "physical mobile", "submitted answers (no model)"]}

    with tempfile.TemporaryDirectory(prefix="recordbench-dock-") as temporary:
        app = create_workbench_app(Path(temporary).resolve() / "runtime", auth_mode="test",
            generator=UnavailableGenerator(), learned_retrieval=False, background_ingestion=False,
            storage_policy=StoragePolicy(reserve_bytes=0))
        prefix, reader = seed(app.state.workbench)
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        base = f"http://127.0.0.1:{listener.getsockname()[1]}"
        server = uvicorn.Server(uvicorn.Config(app, log_level="warning", access_log=False))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()
        options = Options()
        options.binary_location = str(args.chrome_binary)
        if sys.platform == "linux":
            options.add_argument("--no-sandbox")
        for flag in ("--headless=new", "--disable-dev-shm-usage", "--window-size=1440,900"):
            options.add_argument(flag)
        driver = None
        try:
            driver = webdriver.Chrome(service=Service(str(args.chromedriver)), options=options)
            wait = WebDriverWait(driver, 15)
            receipt["browser"] = {"name": driver.capabilities["browserName"], "version": driver.capabilities["browserVersion"]}
            wait.until(lambda _: server.started)
            js = driver.execute_script
            find = lambda selector: driver.find_element(By.CSS_SELECTOR, selector)
            rect = lambda element: js("return arguments[0].getBoundingClientRect().toJSON()", element)

            def viewport(width, height):
                driver.execute_cdp_cmd("Emulation.setDeviceMetricsOverride", {
                    "width": width, "height": height, "deviceScaleFactor": 1, "mobile": False})

            def open_page(path):
                driver.get(base + path)
                wait.until(lambda _: js("return document.readyState") == "complete")
                # A remembered collapsed dock must not hide the controls under test.
                if "assistant-collapsed" in find("body").get_attribute("class"):
                    find("[data-assistant-expand]").click()
                    wait.until(lambda _: "assistant-collapsed" not in find("body").get_attribute("class"))

            def no_page_overflow():
                assert js("return document.documentElement.scrollWidth <= innerWidth"), "page scrolls sideways"

            def header_fits():
                dock = rect(find(".assistant-panel"))
                close = rect(find("[data-assistant-collapse]"))
                assert close["right"] <= dock["right"] + 1 and close["left"] >= dock["left"] - 1, (close, dock)

            def suggestions():
                heading = find("#assistant-suggestions-heading").text
                buttons = driver.find_elements(By.CSS_SELECTOR, "[data-assistant-suggestion]")
                return heading, [button for button in buttons if button.is_displayed()]

            viewport(1440, 900)
            open_page(reader)
            wait.until(lambda _: driver.find_elements(By.CSS_SELECTOR, "[data-assistant-suggestion]"))
            heading, buttons = suggestions()
            assert heading == "Suggested for this source", heading
            assert [button.text for button in buttons] == [
                "Summarize this source", "Who and what appears here", "What dates appear here"]
            scope = lambda: js("const s=document.querySelector('select[name=source_set]');"
                               "return s.selectedOptions[0] ? s.selectedOptions[0].textContent : ''")
            assert scope().startswith("All searchable sources"), scope()
            close = find("[data-assistant-collapse]")
            assert close.text.strip() == "Close" and "Close" in close.get_attribute("aria-label")
            no_page_overflow()
            header_fits()
            driver.save_screenshot(str(args.output / "dock-source-1440.png"))
            checks.append("At 1440px with a source open, the dock offers three questions about that source and a visible Close control")

            # Native keyboard: Tab from the start of the page reaches a suggestion; Enter fills, never sends.
            js("document.activeElement && document.activeElement.blur(); window.scrollTo(0, 0)")
            find("body").send_keys(Keys.TAB)
            for _ in range(250):
                active = driver.switch_to.active_element
                if active.get_attribute("data-assistant-suggestion"):
                    break
                active.send_keys(Keys.TAB)
            else:
                raise AssertionError("No suggestion is reachable with Tab")
            question = active.get_attribute("data-assistant-suggestion")
            active.send_keys(Keys.ENTER)
            textarea = find("#assistant-question")
            wait.until(lambda _: textarea.get_attribute("value") == question)
            # The question was filled only after the next question was limited to this source.
            assert scope() == "Only · " + SOURCE_NAME, scope()
            assert "limited to this source" in find("[data-assistant-suggestion-status]").text
            wait.until(lambda _: driver.switch_to.active_element == textarea)
            assert not find("[data-assistant-status]").is_displayed(), "a suggestion submitted a question"
            assert app.state.workbench.workspace.latest_answer_job(
                app.state.workbench.matter(prefix.split("/")[2], ACTOR).matter_id,
                find("[data-assistant-dock]").get_attribute("data-conversation-id"), ACTOR) is None
            checks.append("Tab reaches a suggestion; Enter limits the next question to this source, then fills and focuses the question box without creating an answer request")

            close = find("[data-assistant-collapse]")
            js("arguments[0].focus()", close)
            close.send_keys(Keys.ENTER)
            wait.until(lambda _: "assistant-collapsed" in find("body").get_attribute("class"))
            expand = find("[data-assistant-expand]")
            wait.until(lambda _: driver.switch_to.active_element == expand)
            expand.send_keys(Keys.ENTER)
            wait.until(lambda _: "assistant-collapsed" not in find("body").get_attribute("class"))
            wait.until(lambda _: driver.switch_to.active_element == find("#assistant-question"))
            assert find("#assistant-question").get_attribute("value") == question
            checks.append("Close and reopen work from the keyboard, move focus to the reopen tab and back to the kept question")

            # A new chat drafted in the browser offers the same source suggestions.
            find("[data-assistant-new-chat]").click()
            wait.until(lambda _: driver.find_elements(By.CSS_SELECTOR, ".assistant-draft-empty [data-assistant-suggestion]"))
            draft = find(".assistant-draft-empty")
            assert draft.find_element(By.CSS_SELECTOR, ".assistant-suggestions-heading").text == "Suggested for this source"
            assert scope().startswith("All searchable sources"), scope()
            # A slow scope response must hold the composer: nothing can be sent or typed meanwhile.
            js("const original = window.fetch; window.fetch = (url, init) => /\\/sources\\/[^/]+\\/ask/.test(String(url))"
               " ? new Promise((resolve) => setTimeout(resolve, 1500)).then(() => original(url, init)) : original(url, init);")
            find("#assistant-question").send_keys("Synthetic unsent draft")
            draft.find_elements(By.CSS_SELECTOR, "[data-assistant-suggestion]")[2].click()
            composer = js("const t=document.querySelector('#assistant-question'), b=t.form.querySelector('button[type=submit]');"
                          "return [t.disabled, b.disabled, t.value, document.querySelector('.assistant-draft-empty .assistant-suggestions').getAttribute('aria-busy')]")
            assert composer == [True, True, "Synthetic unsent draft", "true"], composer
            # A readiness update and a forced submission while pending must not reopen or send.
            held = ("const t=document.querySelector('#assistant-question'), b=t.form.querySelector('button[type=submit]');"
                    "return [t.disabled, b.disabled, t.value]")
            js("window.dispatchEvent(new CustomEvent('recordbench:readiness', {detail: {can_query: true, state: 'ready',"
               " searchable_count: 1, total_count: 1}}))")
            assert js(held) == [True, True, "Synthetic unsent draft"], js(held)
            js("document.querySelector('#assistant-question').form.requestSubmit()")
            assert js(held) == [True, True, "Synthetic unsent draft"], js(held)
            wait.until(lambda _: find("#assistant-question").get_attribute("value")
                       == "Which dates appear in this source, and what happened on each?")
            assert scope() == "Only · " + SOURCE_NAME, scope()
            assert js("const t=document.querySelector('#assistant-question');"
                      "return !t.disabled && !t.form.querySelector('button[type=submit]').disabled")
            assert not app.state.workbench.workspace.answer_jobs_for_actor(ACTOR), "a question was sent while scope was pending"
            ids = js("return [...document.querySelectorAll('[id]')].map(e => e.id)")
            assert len(ids) == len(set(ids)), "duplicate ids after drafting a new chat"
            checks.append("New chat offers the same source suggestions; while a slow scope response is pending the question box, Send and suggestions are held, then the box is filled with the scope applied")

            open_page(prefix + "/notebook")
            wait.until(lambda _: driver.find_elements(By.CSS_SELECTOR, "[data-assistant-suggestion]"))
            heading, buttons = suggestions()
            assert heading == "Suggested questions" and [button.text for button in buttons] == [
                "Summarize the records", "Find people and organizations", "Review dates and events"]
            buttons[0].click()
            wait.until(lambda _: find("#assistant-question").get_attribute("value") == "Summarize the records in this matter.")
            assert scope().startswith("All searchable sources"), scope()
            checks.append("Pages without an open source keep the general suggestions, which fill the box without changing scope")

            for width, height in ((390, 844), (320, 640)):
                viewport(width, height)
                open_page(reader)
                wait.until(lambda _: driver.find_elements(By.CSS_SELECTOR, "[data-assistant-suggestion]"))
                no_page_overflow()
                header_fits()
                for button in suggestions()[1]:
                    box = rect(button)
                    assert box["right"] <= width + 1, (width, box)
                driver.save_screenshot(str(args.output / f"dock-source-{width}.png"))
            checks.append("At 390px and 320px the dock, its Close control and source suggestions fit without sideways scrolling")

            seed_answer(app.state.workbench, prefix)
            viewport(1440, 900)
            open_page(reader)
            suggest = wait.until(lambda _: driver.find_element(By.CSS_SELECTOR, "[data-assistant-suggest-passage] button"))
            assert suggest.text.strip() == "Suggest people, things and dates from this passage", suggest.text
            form = find("[data-assistant-suggest-passage]")
            inbox = form.find_element(By.CSS_SELECTOR, "[data-suggest-inbox]")
            timeline = form.find_element(By.CSS_SELECTOR, "[data-suggest-timeline]")
            assert not inbox.is_displayed() and not timeline.is_displayed()
            matter = app.state.workbench.workspace.get_matter(prefix.rsplit("/", 1)[1], ACTOR)
            service = app.state.workbench.entity_service(matter)
            identity_count = lambda: service.list(matter.matter_id, ACTOR)[1]
            status = form.find_element(By.CSS_SELECTOR, "[data-suggest-passage-status]")
            card = find("[data-suggest-preview-card]")
            js("arguments[0].focus()", suggest)
            suggest.send_keys(Keys.ENTER)
            wait.until(lambda _: card.is_displayed() and card.find_elements(By.CSS_SELECTOR, "li"))
            heading = card.find_element(By.CSS_SELECTOR, "h3")
            assert heading.text == "Suggestions from this passage" and js("return document.activeElement") == heading
            labels = [item.text for item in card.find_elements(By.CSS_SELECTOR, "li")]
            assert any(label.endswith(" · Date") for label in labels), labels
            assert "Nothing is added until you choose Add." in status.text, status.text
            assert card.find_element(By.CSS_SELECTOR, ".suggestion-preview-sources").text.startswith("From: " + SOURCE_NAME)
            assert card.find_element(By.XPATH, ".//button[starts-with(normalize-space(), 'Add ')]").text == f"Add {len(labels)} to suggestions"
            assert identity_count() == 0
            driver.save_screenshot(str(args.output / "dock-suggest-preview-1440.png"))
            dismiss = card.find_element(By.XPATH, ".//button[normalize-space()='Dismiss']")
            js("arguments[0].focus()", dismiss)
            dismiss.send_keys(Keys.ENTER)
            wait.until(lambda _: not card.is_displayed())
            assert js("return document.activeElement") == suggest and status.text == "Dismissed; nothing was added."
            assert identity_count() == 0
            checks.append("Suggest from an answer passage previews what it found and from which passages, from the keyboard; Dismiss adds nothing and returns focus")

            suggest.send_keys(Keys.ENTER)
            add = wait.until(lambda _: card.is_displayed() and card.find_element(By.XPATH, ".//button[starts-with(normalize-space(), 'Add ')]"))
            # A slow Add holds Dismiss and Suggest, so feedback never says nothing was added while it may succeed.
            js("const original = window.fetch; window.fetch = (url, init) => /\\/suggestions\\/claims\\/\\d+$/.test(String(url))"
               " ? new Promise((resolve) => setTimeout(resolve, 1500)).then(() => original(url, init)) : original(url, init);")
            add.click()
            wait.until(lambda _: status.text == "Adding suggestions…")
            dismiss = card.find_element(By.XPATH, ".//button[normalize-space()='Dismiss']")
            assert dismiss.get_attribute("aria-disabled") == "true" and suggest.get_attribute("aria-disabled") == "true"
            dismiss.click()
            suggest.click()
            assert card.is_displayed() and status.text == "Adding suggestions…", status.text
            wait.until(lambda _: status.text.startswith("Added "))
            assert suggest.get_attribute("aria-disabled") is None
            assert "date" in status.text, status.text
            assert inbox.is_displayed() and timeline.is_displayed() and not card.is_displayed()
            assert identity_count() == len(labels)
            assert driver.current_url.startswith(base + reader), driver.current_url
            no_page_overflow()
            driver.save_screenshot(str(args.output / "dock-suggested-1440.png"))
            js("arguments[0].focus()", suggest)
            suggest.send_keys(Keys.ENTER)
            wait.until(lambda _: status.text.startswith("No new people, things or dates"))
            assert not card.is_displayed() and not timeline.is_displayed() and inbox.is_displayed()
            checks.append("While a slow Add is pending, Dismiss and Suggest are held; Add then adds the previewed suggestions without leaving the reader, announces them, offers the inbox and timeline draft, and a repeat finds nothing new")

            inbox.click()
            wait.until(lambda _: driver.find_elements(By.CSS_SELECTOR, "#suggestions .suggestion-from-answer"))
            assert all(tag.text == "From an answer" for tag in driver.find_elements(By.CSS_SELECTOR, ".suggestion-from-answer"))
            open_page(prefix + "/chronology#found-dates-heading")
            assert "2026-03-05" in find(".found-dates").text
            checks.append("The inbox marks suggestions that came from an answer, and the timeline draft lists the found date")

            for width, height in ((390, 844), (320, 640)):
                viewport(width, height)
                open_page(reader)
                form = wait.until(lambda _: driver.find_element(By.CSS_SELECTOR, "[data-assistant-suggest-passage]"))
                assert rect(form.find_element(By.CSS_SELECTOR, "button"))["right"] <= width + 1
                no_page_overflow()
                driver.save_screenshot(str(args.output / f"dock-suggest-{width}.png"))
            # An open proposal naming a source with a long unbroken filename still fits a phone.
            bench = app.state.workbench
            matter = bench.workspace.get_matter(prefix.rsplit("/", 1)[1], ACTOR)
            long_name = "Synthetic-" + "unbroken" * 24 + ".txt"
            long_document, _ = bench.source_store(matter).store_stream(
                long_name, "text/plain", io.BytesIO(b"witness: Morgan Example signed the second log on 2026-04-01."))
            seed_answer(bench, prefix, long_document)
            open_page(prefix + "/notebook")
            last_suggest = wait.until(lambda _: driver.find_elements(By.CSS_SELECTOR, "[data-assistant-suggest-passage] button"))[-1]
            driver.execute_script("arguments[0].scrollIntoView({block: 'center'})", last_suggest)
            last_suggest.click()
            open_card = wait.until(lambda _: next((card for card in driver.find_elements(By.CSS_SELECTOR, "[data-suggest-preview-card]")
                                                   if card.is_displayed() and card.find_elements(By.CSS_SELECTOR, "li")), None))
            sources = open_card.find_element(By.CSS_SELECTOR, ".suggestion-preview-sources")
            assert long_name[:40] in sources.text.replace("\n", "")
            for element in (open_card, sources):
                box = rect(element)
                assert box["left"] >= -1 and box["right"] <= 321, box
            assert js("return arguments[0].scrollWidth <= arguments[0].clientWidth + 1", sources)
            no_page_overflow()
            driver.save_screenshot(str(args.output / "dock-suggest-preview-320.png"))
            checks.append("At 390px and 320px the answer-passage Suggest control fits, and an open proposal naming a long unbroken filename wraps without sideways scrolling")

            # Draft questions: a proposal card that saves nothing until Save.
            # Rendering a draft rebuilds the card, so read it in the page, not through held elements:
            # the drafting request has settled, and the card shows the expected heading and a Save.
            draft_shown = lambda target, label: js(
                "const card = arguments[0]; return Boolean(!card.previousElementSibling?.hasAttribute('aria-busy')"
                " && card.querySelector('h3')?.textContent.startsWith(arguments[1])"
                " && [...card.querySelectorAll('button')].some((b) => b.textContent.trim() === 'Save to case notes'"
                " && b.getAttribute('aria-disabled') !== 'true'));",
                target, label)
            bench = app.state.workbench
            bench.generator.client = SyntheticDrafts()
            # The journey added a second source above, so draft from Case notes, where the dock also opens.
            notes = prefix + "/notebook"
            matter = bench.workspace.get_matter(prefix.rsplit("/", 1)[1], ACTOR)
            saved_notes = lambda: len(bench.workspace.all_notebook_items(matter.matter_id, ACTOR))
            viewport(1440, 900)
            open_page(notes)
            witness = wait.until(lambda _: driver.find_element(By.CSS_SELECTOR, '[data-assistant-draft-questions] button[value="witness"]'))
            js("arguments[0].focus()", witness)
            witness.send_keys(Keys.ENTER)
            card = find("[data-question-draft-card]")
            wait.until(lambda _: card.is_displayed() and card.find_elements(By.CSS_SELECTOR, ".question-draft-list li"))
            heading = card.find_element(By.CSS_SELECTOR, "h3")
            assert heading.text == "Questions for a witness · draft for review", heading.text
            assert js("return document.activeElement") == heading
            items = card.find_elements(By.CSS_SELECTOR, ".question-draft-list li")
            # The invented quotation failed the check and is not shown.
            assert len(items) == 2 and all("Sources: " in item.text for item in items), [item.text for item in items]
            assert "did not pass citation and text checks" in card.text
            assert saved_notes() == 0
            no_page_overflow()
            driver.save_screenshot(str(args.output / "dock-draft-1440.png"))
            dismiss = card.find_element(By.XPATH, ".//button[normalize-space()='Dismiss']")
            js("arguments[0].focus()", dismiss)
            dismiss.send_keys(Keys.ENTER)
            wait.until(lambda _: not card.is_displayed())
            assert js("return document.activeElement") == witness
            assert find("[data-question-draft-status]").text == "Draft dismissed; nothing was saved."
            assert saved_notes() == 0
            checks.append("Draft questions works from the keyboard: the proposal card takes focus, shows only questions that pass the check with their sources, and Dismiss saves nothing and returns focus")

            find('[data-assistant-draft-questions] button[value="discovery"]').click()
            wait.until(lambda _: card.is_displayed() and card.find_elements(By.CSS_SELECTOR, ".question-draft-list li"))
            assert card.find_element(By.CSS_SELECTOR, "h3").text.startswith("Discovery requests")
            # A slow replacement draft holds the open card's Save and Dismiss, so no save is lost with the card.
            js("window.__originalFetch = window.fetch; window.fetch = (url, init) => /\\/questions$/.test(String(url))"
               " ? new Promise((resolve) => setTimeout(resolve, 1500)).then(() => window.__originalFetch(url, init))"
               " : window.__originalFetch(url, init);")
            find('[data-assistant-draft-questions] button[value="witness"]').click()
            wait.until(lambda _: find("[data-question-draft-status]").text.startswith("Drafting from the cited passages"))
            old_save = card.find_element(By.XPATH, ".//button[normalize-space()='Save to case notes']")
            assert old_save.get_attribute("aria-disabled") == "true"
            assert card.find_element(By.XPATH, ".//button[normalize-space()='Dismiss']").get_attribute("aria-disabled") == "true"
            old_save.click()
            assert saved_notes() == 0
            wait.until(lambda _: draft_shown(card, "Questions for a witness"))
            assert saved_notes() == 0
            js("window.fetch = window.__originalFetch;")
            # The second answer's card is open too, so two saves can overlap.
            second_form = driver.find_elements(By.CSS_SELECTOR, "[data-assistant-draft-questions]")[1]
            second_card = driver.find_elements(By.CSS_SELECTOR, "[data-question-draft-card]")[1]
            driver.execute_script("arguments[0].scrollIntoView({block: 'center'})", second_form)
            second_form.find_element(By.CSS_SELECTOR, 'button[value="witness"]').click()
            wait.until(lambda _: second_card.is_displayed() and second_card.find_elements(By.CSS_SELECTOR, ".question-draft-list li"))
            # A slow Save holds Dismiss and the draft buttons until its result is known; the second
            # card's save is slower, so navigation stays held until both are known.
            js("const original = window.fetch; window.fetch = (url, init) => /\\/questions\\/save$/.test(String(url))"
               " ? new Promise((resolve) => setTimeout(resolve, url === arguments[0] ? 4000 : 1500)).then(() => original(url, init))"
               " : original(url, init);", second_card.find_element(By.CSS_SELECTOR, "form").get_attribute("action"))
            card.find_element(By.XPATH, ".//button[normalize-space()='Save to case notes']").click()
            wait.until(lambda _: "Saving…" in card.text)
            second_card.find_element(By.XPATH, ".//button[normalize-space()='Save to case notes']").click()
            wait.until(lambda _: "Saving…" in second_card.text)
            held_dismiss = card.find_element(By.XPATH, ".//button[normalize-space()='Dismiss']")
            assert held_dismiss.get_attribute("aria-disabled") == "true"
            # Conversation navigation is held too, so the dock is not replaced before the result shows.
            assert js("return document.querySelector('[data-assistant-conversation-picker]')?.disabled ?? true")
            new_chat = driver.find_elements(By.CSS_SELECTOR, "[data-assistant-new-chat]")
            assert all(button.get_attribute("aria-disabled") == "true" for button in new_chat)
            held_dismiss.click()
            assert card.is_displayed() and saved_notes() == 0
            wait.until(lambda _: "Questions saved to case notes for review." in card.text)
            assert card.find_element(By.LINK_TEXT, "Open case notes").get_attribute("href").endswith(prefix + "/notebook")
            # The first save finished, but the second is still pending: navigation stays held.
            assert "Saving…" in second_card.text
            assert js("return document.querySelector('[data-assistant-conversation-picker]')?.disabled ?? true")
            assert all(button.get_attribute("aria-disabled") == "true" for button in new_chat)
            wait.until(lambda _: "Questions saved to case notes for review." in second_card.text)
            assert not js("return document.querySelector('[data-assistant-conversation-picker]')?.disabled ?? false")
            assert all(button.get_attribute("aria-disabled") is None for button in new_chat)
            assert saved_notes() == 2
            assert driver.current_url.startswith(base + notes), driver.current_url
            # A dock refresh (here through the dock's Cancel path, answered synthetically) that
            # arrives during a slow save waits for it, then keeps its result and link.
            js("window.fetch = (url, init) => String(url).endsWith('/synthetic-cancel')"
               " ? Promise.resolve(new Response(JSON.stringify({state: 'cancelled', message: 'Request cancelled'}),"
               " {headers: {'Content-Type': 'application/json'}}))"
               " : /\\/questions(\\/save)?$/.test(String(url))"
               " ? new Promise((resolve) => setTimeout(resolve, 1500)).then(() => window.__originalFetch(url, init))"
               " : window.__originalFetch(url, init);")
            press_cancel = ("const dock = document.querySelector('[data-assistant-dock]'); window.__oldDock = dock;"
                            " const button = document.createElement('button'); button.type = 'button';"
                            " button.dataset.assistantCancel = ''; button.dataset.actionUrl = '/synthetic-cancel';"
                            " dock.append(button); button.click();")
            find('[data-assistant-draft-questions] button[value="discovery"]').click()
            # The card still shows the saved witness draft until the slow replacement arrives.
            wait.until(lambda _: draft_shown(card, "Discovery requests"))
            card.find_element(By.XPATH, ".//button[normalize-space()='Save to case notes']").click()
            wait.until(lambda _: "Saving…" in card.text)
            js(press_cancel)
            assert js("return window.__oldDock.isConnected") and "Saving…" in card.text
            wait.until(lambda _: not js("return window.__oldDock.isConnected"))
            carried = find("[data-assistant-draft-questions] [data-question-draft-status]")
            assert "Questions saved to case notes for review." in carried.text, carried.text
            assert carried.find_element(By.LINK_TEXT, "Open case notes").get_attribute("href").endswith(prefix + "/notebook")
            assert saved_notes() == 3
            # And no save starts while a refresh is already in flight.
            find('[data-assistant-draft-questions] button[value="witness"]').click()
            card = find("[data-question-draft-card]")
            wait.until(lambda _: card.is_displayed()
                       and card.find_elements(By.XPATH, ".//button[normalize-space()='Save to case notes']"))
            js("const fragment = document.querySelector('[data-assistant-dock]').dataset.fragmentUrl;"
               " const previous = window.fetch; window.fetch = (url, init) => String(url).includes(fragment)"
               " ? new Promise((resolve) => setTimeout(resolve, 2000)).then(() => window.__originalFetch(url, init))"
               " : previous(url, init);")
            js(press_cancel)
            card.find_element(By.XPATH, ".//button[normalize-space()='Save to case notes']").click()
            assert "The assistant is updating. Try Save again in a moment." in card.text, card.text
            wait.until(lambda _: not js("return window.__oldDock.isConnected"))
            assert saved_notes() == 3
            # The open, unsaved draft is carried into the refreshed dock for review.
            card = find("[data-question-draft-card]")
            wait.until(lambda _: draft_shown(card, "Questions for a witness"))
            draft_status = lambda: find("[data-assistant-draft-questions] [data-question-draft-status]").text
            assert draft_status() == "The chat updated; this draft is still open for review.", draft_status()
            # No draft starts while a refresh is in flight.
            js(press_cancel)
            find('[data-assistant-draft-questions] button[value="discovery"]').click()
            assert draft_status() == "The assistant is updating. Try drafting again in a moment.", draft_status()
            wait.until(lambda _: not js("return window.__oldDock.isConnected"))
            # A refresh that arrives while a slow draft is in flight waits for it, then carries it over.
            js("window.fetch = (url, init) => /\\/questions$/.test(String(url))"
               " ? new Promise((resolve) => setTimeout(resolve, 1500)).then(() => window.__originalFetch(url, init))"
               " : String(url).endsWith('/synthetic-cancel')"
               " ? Promise.resolve(new Response(JSON.stringify({state: 'cancelled', message: 'Request cancelled'}),"
               " {headers: {'Content-Type': 'application/json'}}))"
               " : window.__originalFetch(url, init);")
            find('[data-assistant-draft-questions] button[value="discovery"]').click()
            wait.until(lambda _: draft_status().startswith("Drafting from the cited passages"))
            js(press_cancel)
            assert js("return window.__oldDock.isConnected")
            wait.until(lambda _: not js("return window.__oldDock.isConnected"))
            card = find("[data-question-draft-card]")
            wait.until(lambda _: draft_shown(card, "Discovery requests"))
            assert draft_status() == "The chat updated; this draft is still open for review.", draft_status()
            # Saving the carried card works; this discovery draft was already saved above, so it adds nothing.
            card.find_element(By.XPATH, ".//button[normalize-space()='Save to case notes']").click()
            wait.until(lambda _: "Those questions were already saved to case notes." in card.text)
            assert saved_notes() == 3
            js("window.fetch = window.__originalFetch;")
            checks.append("While a slow replacement draft is pending the open card's Save and Dismiss are held, and while a slow Save is pending Dismiss, conversation navigation and dock refreshes wait until every overlapping save or draft is known, keeping each result and open draft; no save or draft starts during a refresh; each saved draft adds one Suggested case note without leaving the page and links to case notes")

            for width, height in ((390, 844), (320, 640)):
                viewport(width, height)
                open_page(notes)
                witness = wait.until(lambda _: driver.find_element(By.CSS_SELECTOR, '[data-assistant-draft-questions] button[value="witness"]'))
                driver.execute_script("arguments[0].scrollIntoView({block: 'center'})", witness)
                witness.click()
                card = find("[data-question-draft-card]")
                wait.until(lambda _: card.is_displayed() and card.find_elements(By.CSS_SELECTOR, ".question-draft-list li"))
                box = rect(card)
                assert box["left"] >= -1 and box["right"] <= width + 1, (width, box)
                no_page_overflow()
                driver.save_screenshot(str(args.output / f"dock-draft-{width}.png"))
            checks.append("At 390px and 320px the draft actions and proposal card fit without sideways scrolling")

            receipt["passed"] = True
            (args.output / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
            print(json.dumps(receipt))
        except Exception as exc:
            failure_evidence(args.output, driver, receipt, exc)
            raise
        finally:
            if driver is not None:
                driver.quit()
            server.should_exit = True
            thread.join(10)


if __name__ == "__main__":
    main()
