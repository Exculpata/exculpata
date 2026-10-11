#!/usr/bin/env python3
"""Check optional Home briefings using disposable synthetic processing and sources."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
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
from selenium.webdriver.support.ui import WebDriverWait

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from synthetic_browser_environment import isolate_environment
from case_intelligence.generation import UnavailableGenerator
from case_intelligence.intake_receipts import IntakeReceipts
from case_intelligence.workbench import create_workbench_app

ACTOR = "development-taylor-morgan"
SOURCES = (
    ("North/first.txt", "Alex Example met Jordan Sample on 2024-05-06. Amber Cooperative opened.\nAlex Example returned on 2024-05-06."),
    ("North/nested/second.txt", "Alex Example met Riley Placeholder on 2024-05-07."),
    ("Root record.txt", "Jordan Sample returned on 2024-05-06."),
)


@contextmanager
def serve(app, release=None):
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    base = f"http://127.0.0.1:{listener.getsockname()[1]}"
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning", access_log=False))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    try:
        WebDriverWait(server, 20).until(lambda current: current.started)
        yield base
    finally:
        if release is not None:
            release.set()
        server.should_exit = True
        thread.join(15)
        listener.close()


def configured_app(runtime, *, briefing=True, one_box=True):
    isolate_environment()
    os.environ["CASE_INTELLIGENCE_BRIEFING"] = "1" if briefing else "0"
    os.environ["CASE_INTELLIGENCE_ONE_BOX"] = "1" if one_box else "0"
    os.environ["CASE_INTELLIGENCE_AUTOMATIC_DISCOVERY"] = "1"
    return create_workbench_app(runtime, generator=UnavailableGenerator(), auth_mode="test",
        learned_retrieval=False, background_ingestion=True, ingestion_workers=1)


def seed_waiting(bench, release, entered):
    matter = bench.create_matter("Synthetic discovery briefing", "Invented source-linked summary", ACTOR)
    original_index = bench.ingestion.index_document

    def controlled_index(current_matter, document):
        entered.set()
        if not release.wait(60):
            raise RuntimeError("Synthetic processing hold timed out")
        return original_index(current_matter, document)

    bench.ingestion.index_document = controlled_index
    store = bench.source_store(matter)
    for path, text in SOURCES:
        document, _ = store.store_stream(path.rsplit("/", 1)[-1], "text/plain",
            io.BytesIO(text.encode()), relative_path=path, defer_processing=True)
        bench._sync_source_catalog(matter, [document])
        bench.workspace.queue_upload(matter.matter_id, document.document_id)
    receipts = IntakeReceipts(bench.workspace)
    receipt = receipts.create(matter.matter_id, ACTOR, selection_key="a" * 32,
        selection_fingerprint="b" * 64, selected_count=1, eligible_indexes=[],
        collection_name="Synthetic unsupported selection")
    receipts.append(matter.matter_id, ACTOR, receipt["receipt_id"], start=0,
        files=[dict(name="opaque.bin", relative_path="Unsupported/opaque.bin", size=25,
                    media_type="application/octet-stream")], reviewed_states=["unsupported"],
        document_limit=1024, media_limit=2048, malware_scan_mode="off", scanner_ready=True)
    receipts.seal(matter.matter_id, ACTOR, receipt["receipt_id"])
    bench.ingestion.notify()
    return matter


def work_counts(bench, matter):
    with bench.workspace._lock:
        queries = (
            "SELECT COUNT(*) FROM workbench_message m JOIN workbench_conversation c "
            "ON c.conversation_id=m.conversation_id WHERE c.matter_id=? AND m.role='user'",
            "SELECT COUNT(*) FROM workbench_answer_job WHERE matter_id=?",
            "SELECT COUNT(*) FROM workbench_research_job WHERE matter_id=?",
        )
        return tuple(bench.workspace.connection.execute(query, (matter.matter_id,)).fetchone()[0]
                     for query in queries)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chrome-binary", type=Path, required=True)
    parser.add_argument("--chromedriver", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.output.is_symlink() or (args.output.exists() and (
            not args.output.is_dir() or any(args.output.iterdir()))):
        raise ValueError("Choose a fresh or empty output directory; existing results were preserved.")
    args.output.mkdir(parents=True, exist_ok=True)
    report = {"synthetic_only": True, "passed": False, "checks": []}
    checks = report["checks"]
    options = Options()
    options.binary_location = str(args.chrome_binary)
    for flag in ("--headless=new", "--no-sandbox", "--disable-dev-shm-usage",
                 "--no-proxy-server", "--window-size=1440,1000"):
        options.add_argument(flag)
    driver = None
    release, entered = threading.Event(), threading.Event()
    try:
        driver = webdriver.Chrome(service=Service(str(args.chromedriver)), options=options)
        driver.set_page_load_timeout(30)
        driver.set_script_timeout(30)
        wait = WebDriverWait(driver, 30)

        def find(selector):
            return driver.find_element(By.CSS_SELECTOR, selector)

        def js(script, *values):
            return driver.execute_script(script, *values)

        def native_focus(element):
            previous = None

            def visible(_):
                nonlocal previous
                measurement = js("""const e=arguments[0],r=e.getBoundingClientRect();
                    const dock=document.querySelector('[data-assistant-expand]');
                    const d=dock?.getBoundingClientRect();
                    const bottom=d && dock.getClientRects().length && d.width>innerWidth/2
                        && d.bottom>=innerHeight-2 ? d.top : innerHeight;
                    return {top:r.top,bottom:r.bottom,left:r.left,right:r.right,scroll_y:scrollY,
                        available_top:document.querySelector('.topbar')?.getBoundingClientRect().bottom || 0,
                        available_bottom:bottom,width:innerWidth,focused:document.activeElement===e,
                        unobscured:e.contains(document.elementFromPoint(r.left+r.width/2,r.top+r.height/2))};""", element)
                report["last_focus_geometry"] = measurement
                geometry = (measurement["top"], measurement["bottom"], measurement["scroll_y"])
                stable = previous is not None and all(abs(now - old) <= .5 for now, old in zip(geometry, previous))
                previous = geometry
                return (stable and measurement["focused"] and measurement["unobscured"]
                    and measurement["top"] >= measurement["available_top"] - 1
                    and measurement["bottom"] <= measurement["available_bottom"] + 1
                    and measurement["left"] >= -1 and measurement["right"] <= measurement["width"] + 1)

            wait.until(visible, "The focused briefing control is clipped or obscured")

        def keyboard_to(element):
            for _ in range(160):
                if driver.switch_to.active_element == element:
                    assert js("return arguments[0].matches(':focus-visible')", element)
                    native_focus(element)
                    return
                ActionChains(driver).send_keys(Keys.TAB).perform()
            raise AssertionError("Briefing control was absent from native Tab order")

        def no_overflow():
            wait.until(lambda _: js("return document.documentElement.scrollWidth <= innerWidth"))

        def choose_question(bench, matter, target_selector):
            before = work_counts(bench, matter)
            choice = find("[data-briefing-question]")
            text = choice.get_attribute("value")
            assert text and "Suggested" in choice.text
            keyboard_to(choice)
            choice.send_keys(Keys.ENTER)
            wait.until(lambda _: find(target_selector).get_attribute("value") == text)
            target = find(target_selector)
            assert target.is_displayed() and target.is_enabled()
            native_focus(target)
            assert work_counts(bench, matter) == before == (0, 0, 0)
            assert urlsplit(driver.current_url).path == f"/matters/{matter.slug}/home"
            return text

        with tempfile.TemporaryDirectory(prefix="recordbench-briefing-") as temporary:
            runtime = Path(temporary).resolve() / "runtime"
            app = configured_app(runtime)
            bench = app.state.workbench
            matter = seed_waiting(bench, release, entered)
            prefix = f"/matters/{matter.slug}"
            with serve(app, release) as base:
                wait.until(lambda _: entered.is_set())
                assert bench.workspace.matter_readiness(matter.matter_id).processing_count > 0
                driver.get(base + prefix + "/home")
                assert not driver.find_elements(By.CSS_SELECTOR, "[data-discovery-briefing]")
                assert "prepar" in find("main").text.casefold() or "processing" in find("main").text.casefold()
                checks.append("Active durable processing retains the existing readiness presentation and suppresses the briefing")
                release.set()
                wait.until(lambda _: bench.workspace.matter_readiness(matter.matter_id).processing_count == 0)
                wait.until(lambda _: js("return !!document.querySelector('[data-discovery-briefing]')"))
                checks.append("Finishing real text extraction, indexing and automatic discovery reveals the briefing without manual reload")
                service = bench.entity_service(matter)
                entities, _ = service.list(matter.matter_id, ACTOR)
                place = next(item for item in entities if item["display_name"] == "Amber Cooperative")
                service.update(matter.matter_id, ACTOR, place["entity_id"], expected_revision=place["revision"],
                    display_name="Amber Cooperative", entity_type="place", status="suggested")
                driver.get(base + prefix + "/home")
                sections = driver.find_elements(By.CSS_SELECTOR, "[data-briefing-section]")
                assert [section.get_attribute("data-briefing-section") for section in sections] == [
                    "arrived", "people_places", "dates", "unread"]
                assert js("return document.querySelector('[data-discovery-briefing]').compareDocumentPosition(document.querySelector('[data-task-launcher]')) & Node.DOCUMENT_POSITION_FOLLOWING")
                arrived = sections[0].text
                for expected in ("3 sources in the source inventory", "File type: TXT — 3 sources",
                                 "Top-level folder: North — 2 sources", "Root (no folder) — 1 sources",
                                 "Earliest and latest document dates are unavailable"):
                    assert expected in arrived, expected
                checks.append("All four briefing sections precede the entry box; file, folder and source counts are exact and document dates remain explicitly unavailable")
                people = sections[1].text
                for expected in ("Suggested person: Alex Example — 3 recorded mentions", "Jordan Sample — 2 recorded mentions",
                                 "Suggested place: Amber Cooperative — 1 recorded mentions"):
                    assert expected in people, expected
                assert "Suggested date: 2024-05-06 — 3 found mentions" in sections[2].text
                assert "Suggested date: 2024-05-07 — 1 found mentions" in sections[2].text
                checks.append("Suggested people, places and busiest mentioned dates retain deterministic frequencies and Suggested labels")
                assert "Unsupported/opaque.bin" in sections[3].text
                assert "Browser-reported selection review:" in sections[3].text
                checks.append("The unread section retains the unsupported intake item and its recorded reason provenance")
                coverage = find("[data-briefing-coverage]")
                assert not coverage.get_attribute("open")
                for line in driver.find_elements(By.CSS_SELECTOR, "[data-briefing-line]"):
                    assert line.find_elements(By.CSS_SELECTOR, "a[href]"), line.text
                questions = driver.find_elements(By.CSS_SELECTOR, "[data-briefing-question]")
                assert len(questions) == 5
                assert all(question.get_attribute("type") == "button" for question in questions)
                checks.append("Every briefing line has source links, coverage begins collapsed, and all five questions require an explicit non-submit choice")

                summary = coverage.find_element(By.CSS_SELECTOR, "summary")
                keyboard_to(summary)
                summary.send_keys(Keys.ENTER)
                assert coverage.get_attribute("open")
                links = js("return [...document.querySelectorAll('[data-discovery-briefing] a[href]')].map(e=>({label:e.textContent.trim(),href:e.getAttribute('href')}))")
                unique = {}
                for link in links:
                    location = urlsplit(link["href"])
                    assert not location.scheme and not location.netloc
                    assert location.path == prefix or location.path.startswith(prefix + "/")
                    assert link["label"]
                    unique.setdefault(link["href"], link["label"])
                assert unique
                # Check every emitted link's response, then open every distinct
                # destination in the actual browser (duplicate examples share URLs).
                for href, label in unique.items():
                    response = driver.execute_async_script("""const done=arguments[arguments.length-1];
                        fetch(arguments[0],{credentials:'same-origin'}).then(async r=>done({
                            status:r.status,type:r.headers.get('content-type'),body:await r.text()}))
                        .catch(e=>done({error:String(e)}));""", href)
                    assert response.get("status") == 200 and "text/html" in response.get("type", ""), (href, response.get("status"))
                    driver.get(base + href)
                    # Chrome may acknowledge navigation before the destination's
                    # DOM is observable. Keep the landmark requirement and wait
                    # for the real page, including filtered source-list links.
                    wait.until(lambda _: driver.find_elements(By.CSS_SELECTOR, "main"), href)
                    assert "Internal Server Error" not in find("body").text
                    if "/sources/" in urlsplit(href).path:
                        assert label in find("main").text
                    if "support" in parse_qs(urlsplit(href).query):
                        assert find(".support-pane").is_displayed()
                report["links_checked"] = len(links)
                report["unique_destinations_opened"] = len(unique)
                checks.append("Every rendered source and coverage link stays in this matter, returns HTML 200 and opens its existing browser destination")

                # Deliver two real Home responses out of order relative to
                # controlled readiness events; no application helper is mocked.
                driver.get(base + prefix + "/home")
                draft = "Synthetic unsent briefing draft"
                field = find("#one-box-question")
                field.send_keys(draft)
                kept_focus = find('[data-task-launcher] a[href*="/setup"]')
                keyboard_to(kept_focus)
                readiness = driver.execute_async_script("""const done=arguments[arguments.length-1];
                    fetch(arguments[0]).then(r=>r.json()).then(done);""", prefix + "/processing-status")
                assert readiness["can_query"]
                js("""const originalFetch=window.fetch;
                    const state={calls:0,pending:{},staleInstalled:false,restore:()=>{window.fetch=originalFetch;state.observer.disconnect();}};
                    document.querySelector('[data-briefing-region]').dataset.syntheticBriefingFetch='original';
                    state.observer=new MutationObserver(records=>{
                        for(const record of records) for(const node of record.addedNodes){
                            if(node.nodeType===1 && (node.matches('[data-synthetic-briefing-fetch="1"]')
                                || node.querySelector('[data-synthetic-briefing-fetch="1"]'))) state.staleInstalled=true;
                        }
                    });
                    state.observer.observe(document.querySelector('main'),{childList:true,subtree:true});
                    window.syntheticBriefingRace=state;
                    const home=arguments[0];
                    window.fetch=(input,options)=>{
                        if(new URL(typeof input==='string'?input:input.url,location.href).pathname!==home)
                            return originalFetch(input,options);
                        const ordinal=++state.calls;
                        return originalFetch(input,options).then(async response=>{
                            const body=(await response.text()).replace('data-briefing-region',
                                'data-briefing-region data-synthetic-briefing-fetch="'+ordinal+'"');
                            const held=new Response(body,{status:response.status,headers:response.headers});
                            return new Promise(resolve=>{state.pending[ordinal]=()=>resolve(held);});
                        });
                    };""", prefix + "/home")
                processing = {**readiness, "state": "preparing", "processing_count": 1,
                              "overview_processing_count": 1, "can_query": False}
                for payload in (processing, readiness):
                    js("window.dispatchEvent(new CustomEvent('recordbench:readiness',{detail:arguments[0]}))", payload)
                wait.until(lambda _: js("return !!window.syntheticBriefingRace.pending[1]"))
                assert find("[data-briefing-region]").get_attribute("hidden")
                for payload in (processing, readiness):
                    js("window.dispatchEvent(new CustomEvent('recordbench:readiness',{detail:arguments[0]}))", payload)
                js("window.syntheticBriefingRace.pending[1]()")
                wait.until(lambda _: js("return !!window.syntheticBriefingRace.pending[2]"))
                assert js("return document.querySelector('[data-briefing-region]').dataset.syntheticBriefingFetch") == "original"
                assert not js("return window.syntheticBriefingRace.staleInstalled")
                js("window.syntheticBriefingRace.pending[2]()")
                wait.until(lambda _: js("return document.querySelector('[data-briefing-region]').dataset.syntheticBriefingFetch==='2'"))
                assert not find("[data-briefing-region]").get_attribute("hidden")
                assert not js("return window.syntheticBriefingRace.staleInstalled")
                assert find("#one-box-question").get_attribute("value") == draft
                assert driver.switch_to.active_element == kept_focus
                assert work_counts(bench, matter) == (0, 0, 0)
                js("window.syntheticBriefingRace.restore(); delete window.syntheticBriefingRace")
                checks.append("An overlapping ready/processing cycle discards the held stale Home response, fetches a fresh briefing and preserves the unsent draft and outside focus")

                driver.execute_cdp_cmd("Emulation.setDeviceMetricsOverride", {
                    "width": 430, "height": 932, "deviceScaleFactor": 1, "mobile": False})
                driver.get(base + prefix + "/home")
                no_overflow()
                assert js("return innerWidth") == 430
                chosen = choose_question(bench, matter, "#one-box-question")
                no_overflow()
                driver.save_screenshot(str(args.output / "briefing-question-430.png"))
                assert find("#one-box-question").get_attribute("value") == chosen
                checks.append("At 430 pixels, native Tab and Enter fill and focus the unobscured one box without submitting or creating answer/research work")
                driver.get(base + prefix + "/home")
                heading = find("[data-discovery-briefing]")
                js("arguments[0].scrollIntoView({block:'start',behavior:'instant'})", heading)
                no_overflow()
                driver.save_screenshot(str(args.output / "briefing-sections-430.png"))
                checks.append("The briefing remains within the 430-pixel viewport without horizontal overflow")

            app = configured_app(runtime, one_box=False)
            bench = app.state.workbench
            matter = bench.matter(matter.slug, ACTOR)
            with serve(app) as base:
                driver.get(base + prefix + "/home")
                assert not driver.find_elements(By.CSS_SELECTOR, "#one-box-question")
                assert "Ask a focused question" in find("[data-task-launcher]").text
                choose_question(bench, matter, "#assistant-question")
                no_overflow()
                driver.save_screenshot(str(args.output / "briefing-ask-fallback-430.png"))
                checks.append("With one box disabled, a keyboard-selected suggestion opens and fills the existing Ask composer without sending it")

            app = configured_app(runtime, briefing=False)
            with serve(app) as base:
                driver.get(base + prefix + "/home")
                assert not driver.find_elements(By.CSS_SELECTOR, "[data-discovery-briefing], [data-briefing-question]")
                assert find("#one-box-question").is_displayed()
                checks.append("Disabling the briefing flag preserves Home's existing optional one-box entry point")
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
                (args.output / "failure.html").write_text(driver.page_source)
            except Exception as capture_error:
                report["screenshot_error"] = type(capture_error).__name__
        (args.output / "receipt.json").write_text(json.dumps(report, indent=2) + "\n")
        raise
    finally:
        release.set()
        if driver is not None:
            driver.quit()


if __name__ == "__main__":
    main()
