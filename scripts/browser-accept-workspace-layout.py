#!/usr/bin/env python3
"""Check shared alignment and independent scrolling using synthetic records."""

from __future__ import annotations

from case_intelligence.legacy_names import (
    LEGACY_STEM,
)

import argparse
import io
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
from urllib.parse import urlsplit

import uvicorn
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.actions.wheel_input import ScrollOrigin
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import Select, WebDriverWait

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from case_intelligence.generation import UnavailableGenerator
from case_intelligence.managed_storage import StoragePolicy
from case_intelligence.workbench import create_workbench_app

ACTOR = "development-taylor-morgan"
SOURCE_NAME = "Synthetic source — North Annex receiving notes with a deliberately long recognizable filename.txt"
SOURCE_TEXT = "The blue crate arrived at the North Annex at 09:15."


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
    for index in range(36):
        bench.create_matter(f"Synthetic workspace {index:02}", "Invented layout acceptance", ACTOR)
    matter = bench.create_matter("Synthetic long review", "Invented records for layout acceptance", ACTOR)
    document, _ = bench.source_store(matter).store_stream(SOURCE_NAME, "text/plain",
        io.BytesIO(SOURCE_TEXT.encode()))
    for index in range(32):
        bench.workspace.create_notebook_item(matter.matter_id, ACTOR, item_type="note", status="needs_review",
            title=f"Synthetic observation {index:02}", body="The invented reviewer is checking the blue crate against the source record. " * 18)
    for index in range(28):
        conversation = bench.workspace.create_conversation(matter.matter_id, f"Synthetic conversation {index:02}", actor_id=ACTOR)
    for index in range(16):
        bench.workspace.append_message(matter.matter_id, conversation.conversation_id, "user", f"What does synthetic passage {index + 1} say?")
        bench.workspace.append_message(matter.matter_id, conversation.conversation_id, "assistant",
            "This invented response exercises a long review page. " * 6)
    candidate = bench._candidate(matter, document, document.parsed_units()[0], 1)
    return f"/matters/{matter.slug}", conversation.conversation_id, bench._support_token(candidate)


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--chrome-binary", type=Path, required=True)
    parser.add_argument("--chromedriver", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    prepare_output(args.output)
    from synthetic_browser_environment import isolate_environment
    isolate_environment()
    checks, alignment, conversation_layouts, measurements, viewports = [], {}, {}, {}, []
    receipt = {"passed": False, "synthetic_only": True,
        "provenance": "temporary synthetic records; no model or live storage",
        "commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        "working_tree_clean": not bool(subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT, text=True).strip()),
        "checks": checks, "left_edges": alignment, "conversation_layouts": conversation_layouts,
        "measurements": measurements, "viewports": viewports,
        "unrun_checks": ["native browser zoom", "OS text scaling", "WebKit", "physical mobile", "screen reader", "authenticated sign-out"]}

    with tempfile.TemporaryDirectory(prefix=f"{LEGACY_STEM}-layout-") as temporary:
        app = create_workbench_app(Path(temporary).resolve() / "runtime", auth_mode="test",
            generator=UnavailableGenerator(), learned_retrieval=False, background_ingestion=False,
            storage_policy=StoragePolicy(reserve_bytes=0))
        prefix, conversation, support = seed(app.state.workbench)
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        base = f"http://127.0.0.1:{listener.getsockname()[1]}"
        server = uvicorn.Server(uvicorn.Config(app, log_level="warning", access_log=False))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()
        options = Options()
        options.binary_location = str(args.chrome_binary)
        # Match the existing disposable journeys on hosted Linux runners.
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
            find = lambda selector: driver.find_element(By.CSS_SELECTOR, selector)
            js = driver.execute_script
            rect = lambda element: js("return arguments[0].getBoundingClientRect().toJSON()", element)
            scroll = lambda element: js("return arguments[0].scrollTop", element)

            def viewport(width, height):
                if f"{width}x{height}" not in viewports:
                    viewports.append(f"{width}x{height}")
                driver.execute_cdp_cmd("Emulation.setDeviceMetricsOverride", {
                    "width": width, "height": height, "deviceScaleFactor": 1, "mobile": False})

            def settle_fonts():
                # Geist swaps in after first paint. The section navigator
                # detaches when that swap moves the tab bar across its content
                # breakpoint, so measurements wait until the faces have loaded.
                wait.until(lambda _: js("return document.fonts.status === 'loaded'"))

            def open_page(suffix):
                driver.get(base + prefix + suffix)
                settle_fonts()

            def go(suffix):
                open_page(suffix)
                # Use the existing dock control so all pages expose their main
                # working area; this preference is retained across navigation.
                buttons = driver.find_elements(By.CSS_SELECTOR, "[data-assistant-collapse]")
                if buttons and buttons[0].is_displayed():
                    buttons[0].click()
                wait.until(lambda _: js("return document.documentElement.scrollWidth <= innerWidth"))

            def wheel(x, y, amount):
                ActionChains(driver).scroll_from_origin(ScrollOrigin.from_viewport(round(x), round(y)), 0, amount).perform()

            def screenshot(name):
                driver.save_screenshot(str(args.output / f"{name}.png"))

            paths = {"review": f"?conversation={conversation}", "work-product": "/work-product", "notes": "/notebook"}
            for width, height in ((1800, 1000), (1440, 480), (390, 844)):
                viewport(width, height)
                edges = []
                for name, suffix in paths.items():
                    go(suffix)
                    compact_navigation = driver.find_elements(By.CSS_SELECTOR, ".matter-section-disclosure")
                    navigation = next((element for element in compact_navigation if element.is_displayed()),
                        find("main .matter-section-tabs"))
                    edge = rect(navigation)["left"]
                    edges.append(edge)
                    screenshot(f"{name}-{width}x{height}")
                    if width == 390 and name == "notes":
                        assert js("return getComputedStyle(arguments[0]).position", find(".notebook-tools")) == "static"
                        assert js("return arguments[0].clientHeight >= arguments[0].scrollHeight - 1", find(".notebook-tools"))
                assert max(edges) - min(edges) <= 1, edges
                alignment[f"{width}x{height}"] = edges
            checks.append("Review, Work product and Case notes share a left edge at wide, short and mobile sizes")
            checks.append("Narrow Case notes tools stay in ordinary page flow without a nested scroll viewport")

            for height in (900, 480):
                viewport(1440, height)
                go(paths["review"])
                pane = find(".workspace-main")
                wheel(1200, height // 2, 1900)
                wait.until(lambda _: scroll(pane) > 1000)
                history = find(".conversation-history")
                history_bounds = rect(history)
                assert history_bounds["top"] >= rect(find(".topbar"))["bottom"]
                assert history_bounds["bottom"] <= rect(find(".composer-wrap"))["top"] - 5
                nav = find("[data-conversation-list]")
                previous_pane = scroll(pane)
                bounds = rect(nav)
                wheel(bounds["right"] - 8, (bounds["top"] + bounds["bottom"]) / 2, 700)
                wait.until(lambda _: scroll(nav) > 0)
                assert abs(scroll(pane) - previous_pane) <= 1
                nav.send_keys(Keys.END)
                wait.until(lambda _: js("return arguments[0].scrollTop + arguments[0].clientHeight >= arguments[0].scrollHeight - 2", nav))
                last = nav.find_elements(By.CSS_SELECTOR, "a")[-1]
                assert rect(last)["bottom"] <= rect(nav)["bottom"] + 1
                screenshot(f"review-independent-history-{height}")

                go(paths["notes"])
                wheel(1200, height // 2, 1900)
                wait.until(lambda _: js("return scrollY") > 1000)
                tools = find(".notebook-tools")
                assert js("return getComputedStyle(arguments[0]).position", tools) == "static"
                assert js("return getComputedStyle(arguments[0]).overflowY", tools) == "visible"
                assert js("return arguments[0].clientHeight >= arguments[0].scrollHeight - 1", tools)
                # Tools move with the document, so a wheel over them must scroll
                # the page rather than trap input inside a second viewport.
                js("arguments[0].scrollIntoView({block:'start',behavior:'instant'})", tools)
                previous_page = js("return scrollY")
                bounds = rect(tools)
                wheel(bounds["right"] - 8, min(height - 30, rect(find(".topbar"))["bottom"] + 80), 500)
                wait.until(lambda _: js("return scrollY") > previous_page + 100)
                assert scroll(tools) == 0
                # Ordinary keyboard traversal must reveal the discovery action
                # below the manual-note form at both normal and short heights.
                find(".suggestion-tool select").send_keys(Keys.TAB)
                suggestion = find(".suggestion-tool button")
                def discovery_visible(_):
                    # Native focus scrolling can land on a fractional CSS pixel.
                    # Measure one frame, allowing the same 1px rounding tolerance
                    # used elsewhere while also checking the center is unobscured.
                    result = js("""const e=arguments[0],r=e.getBoundingClientRect();
                        const top=document.querySelector('.topbar').getBoundingClientRect().bottom;
                        const hit=document.elementFromPoint(r.left+r.width/2,r.top+r.height/2);
                        return {focused:document.activeElement===e,top:r.top,bottom:r.bottom,
                            topbar_bottom:top,viewport_height:innerHeight,
                            unobscured:e.contains(hit)};""", suggestion)
                    measurements[f"notes_discovery_focus_{height}"] = result
                    return (result["focused"] and result["unobscured"]
                        and result["top"] >= result["topbar_bottom"] - 1
                        and result["bottom"] <= result["viewport_height"] + 1)
                wait.until(discovery_visible)

                rail, matter_list = find(".matter-rail"), find(".matter-list")
                rail_before = rect(rail)
                previous_page = js("return scrollY")
                bounds = rect(matter_list)
                wheel(bounds["right"] - 8, (bounds["top"] + bounds["bottom"]) / 2, 1600)
                wait.until(lambda _: scroll(matter_list) > 0)
                matter_list.send_keys(Keys.END)
                wait.until(lambda _: js("return arguments[0].scrollTop + arguments[0].clientHeight >= arguments[0].scrollHeight - 2", matter_list))
                assert rect(matter_list.find_elements(By.CSS_SELECTOR, "a")[-1])["bottom"] <= rect(matter_list)["bottom"] + 1
                assert abs(js("return scrollY") - previous_page) <= 1
                assert rect(rail) == rail_before
                assert rect(find("[data-rail-collapse]"))["bottom"] <= height
                screenshot(f"notes-and-matter-rail-independent-{height}")
            checks.append("Native wheel scrolls long right-hand content while Review history remains above the composer")
            checks.append("Native wheel and End key reach the bottom of conversation history without moving the review pane")
            checks.append("Case notes tools scroll with the document; native Tab reaches discovery without a nested scroll viewport")
            checks.append("37-matter rail scrolls independently, remains fixed and keeps its bottom control reachable")
            for width in (761, 820, 900):
                viewport(width, 900)
                go(paths["review"])
                pane = find(".workspace-main")
                # Exercise the space consumed by classic scrollbars on Linux
                # and by accessibility scrollbar preferences on other hosts.
                js("const s=document.createElement('style');s.textContent='.workspace-main{scrollbar-width:auto;overflow-x:scroll}.workspace-main::-webkit-scrollbar{height:24px;width:24px}';document.head.append(s)")
                assert js("return arguments[0].clientHeight < arguments[0].getBoundingClientRect().height", pane)
                wheel(width - 30, 300, 1900)
                wait.until(lambda _: scroll(pane) > 1000)
                history, active = find(".conversation-history"), find(".conversation-active")
                composer = find(".composer-wrap")
                history_bounds, active_bounds = rect(history), rect(active)
                assert history_bounds["right"] + 16 <= active_bounds["left"]
                wait.until(lambda _: rect(history)["bottom"] <= rect(composer)["top"] - 5)
                history_bounds = rect(history)
                assert js("const r=arguments[0].getBoundingClientRect(); return arguments[0].contains(document.elementFromPoint(r.left+r.width/2, arguments[1]))", active, history_bounds["top"] + 100)
                conversation_layouts[str(width)] = {
                    "columns": js("return getComputedStyle(arguments[0]).gridTemplateColumns", find(".conversation-workspace")),
                    "history_right": history_bounds["right"], "active_left": active_bounds["left"],
                    "history_bottom": history_bounds["bottom"], "composer_top": rect(composer)["top"],
                }
                nav = find("[data-conversation-list]")
                previous_pane, bounds = scroll(pane), rect(nav)
                # Wheel the list content. The right edge is the scrollbar gutter, and a
                # wheel there can leave the list mid-scroll so End never becomes its key.
                wheel((bounds["left"] + bounds["right"]) / 2, (bounds["top"] + bounds["bottom"]) / 2, 700)
                seen = []
                def list_wheel_settled(_):
                    seen.append(scroll(nav))
                    return len(seen) >= 2 and seen[-1] > 0 and seen[-1] == seen[-2]
                wait.until(list_wheel_settled)
                js("arguments[0].focus({preventScroll:true})", nav)
                nav.send_keys(Keys.END)
                wait.until(lambda _: js("return arguments[0].scrollTop + arguments[0].clientHeight >= arguments[0].scrollHeight - 2", nav))
                assert abs(scroll(pane) - previous_pane) <= 1
                screenshot(f"review-support-closed-{width}")
            checks.append("Classic scrollbars reduce the available review pane height without obscuring conversation history behind the composer")
            viewport(760, 900)
            go(paths["review"])
            pane, history, active, nav = find(".workspace-main"), find(".conversation-history"), find(".conversation-active"), find("[data-conversation-list]")
            assert js("return getComputedStyle(arguments[0]).position", history) == "static"
            assert js("return getComputedStyle(arguments[0]).maxHeight", nav) == "250px"
            assert rect(history)["bottom"] <= rect(active)["top"]
            assert abs(rect(history)["left"] - rect(active)["left"]) <= 1
            wheel(730, 300, 1900)
            wait.until(lambda _: scroll(pane) > 1000)
            assert rect(history)["bottom"] <= rect(find(".topbar"))["bottom"]
            assert js("const r=arguments[0].getBoundingClientRect(); return arguments[0].contains(document.elementFromPoint(r.left+r.width/2, 200))", active)
            conversation_layouts["760"] = {"history_position": "static", "list_max_height": "250px", "history_bottom_after_scroll": rect(history)["bottom"]}
            screenshot("review-support-closed-760-stacked")
            checks.append("Without support, 761/820/900px history stays beside clickable active content and scrolls independently; at760px capped static history scrolls away above the conversation")
            def storage_banner():
                banner = find(".evaluation-node-banner")
                offset = js("return getComputedStyle(document.body).getPropertyValue('--evaluation-banner-offset').trim()")
                assert banner.is_displayed()
                assert banner.text == "Test node: no storage reserve, not for real case data"
                assert offset == "38px", offset
                return banner, offset

            for width in (761, 820, 900):
                viewport(width, 900)
                go(paths["review"] + f"&support={support}")
                pane = find(".workspace-main")
                # Scroll the review itself, above the open support drawer.
                wheel(width - 30, 180, 1900)
                wait.until(lambda _: scroll(pane) > 1000)
                history, composer, drawer = find(".conversation-history"), find(".composer-wrap"), find(".support-pane")
                banner, offset = storage_banner()
                shell = find(".workspace-shell")
                assert rect(shell)["top"] >= rect(banner)["bottom"] - 1
                assert rect(shell)["bottom"] <= js("return innerHeight") + 1, (width, rect(shell), offset)
                assert rect(history)["top"] >= rect(find(".topbar"))["bottom"]
                assert rect(history)["bottom"] <= rect(composer)["top"] - 5, (width, rect(history), rect(composer))
                composer_box, drawer_box = rect(composer), rect(drawer)
                assert composer_box["bottom"] <= drawer_box["top"] + 1, (width, composer_box, drawer_box, offset)
                measurements[f"support_drawer_{width}"] = {
                    "banner_offset": offset,
                    "composer_bottom": composer_box["bottom"],
                    "drawer_top": drawer_box["top"],
                    "shell_bottom": rect(shell)["bottom"],
                }
                nav = find("[data-conversation-list]")
                bounds, previous_pane = rect(nav), scroll(pane)
                wheel((bounds["left"] + bounds["right"]) / 2, (bounds["top"] + bounds["bottom"]) / 2, 700)
                seen = []
                def open_list_settled(_):
                    seen.append(scroll(nav))
                    return len(seen) >= 2 and seen[-1] > 0 and seen[-1] == seen[-2]
                wait.until(open_list_settled)
                js("arguments[0].focus({preventScroll:true})", nav)
                nav.send_keys(Keys.END)
                wait.until(lambda _: js("return arguments[0].scrollTop + arguments[0].clientHeight >= arguments[0].scrollHeight - 2", nav))
                assert abs(scroll(pane) - previous_pane) <= 1
                assert rect(nav.find_elements(By.CSS_SELECTOR, "a")[-1])["bottom"] <= rect(nav)["bottom"] + 1
                screenshot(f"review-support-open-{width}")
            viewport(820, 650)
            # Resize the open page without navigation: the drawer offset changes
            # independently of the composer's intrinsic dimensions. The storage
            # banner stays on, so this width still has to clear the drawer.
            wait.until(lambda _: rect(find(".conversation-history"))["bottom"] <= rect(find(".composer-wrap"))["top"] - 5)
            _, offset = storage_banner()
            composer_box, drawer_box = rect(find(".composer-wrap")), rect(find(".support-pane"))
            assert composer_box["bottom"] <= drawer_box["top"] + 1, (composer_box, drawer_box, offset)
            assert rect(find(".workspace-shell"))["bottom"] <= js("return innerHeight") + 1
            measurements["support_drawer_820x650"] = {
                "banner_offset": offset,
                "composer_bottom": composer_box["bottom"],
                "drawer_top": drawer_box["top"],
            }
            history, nav = find(".conversation-history"), find("[data-conversation-list]")
            previous_pane = scroll(find(".workspace-main"))
            bounds = rect(history)
            wheel(bounds["right"] - 8, (bounds["top"] + bounds["bottom"]) / 2, 1900)
            nav.send_keys(Keys.END)
            last = nav.find_elements(By.CSS_SELECTOR, "a")[-1]
            wait.until(lambda _: rect(last)["bottom"] <= rect(history)["bottom"] + 1)
            assert rect(last)["top"] >= rect(history)["top"]
            assert js("const r=arguments[0].getBoundingClientRect(); return arguments[0].contains(document.elementFromPoint(r.left+r.width/2,r.top+r.height/2))", last)
            assert abs(scroll(find(".workspace-main")) - previous_pane) <= 1
            screenshot("review-support-open-short-tablet")
            checks.append("Tablet support drawer and composer leave conversation navigation independently scrollable at 761, 820 and 900 pixels with the test-node banner on")
            checks.append("Resizing open support to 820x650 keeps the whole history scrollable and its final link clickable above the composer")
            def no_page_overflow():
                assert js("return document.documentElement.scrollWidth <= innerWidth + 1"), "Page scrolls sideways"

            def fits(element, container=None):
                bounds = rect(element)
                enclosing = rect(container) if container is not None else {"left": 0, "right": js("return innerWidth")}
                assert bounds["width"] > 0 and bounds["height"] > 0, (element.tag_name, bounds)
                assert bounds["left"] >= enclosing["left"] - 1 and bounds["right"] <= enclosing["right"] + 1, (bounds, enclosing)
                assert js("return arguments[0].scrollWidth <= arguments[0].clientWidth + 1", element), element.get_attribute("class")
                return bounds

            def reachable(element):
                js("arguments[0].scrollIntoView({block:'center',inline:'nearest',behavior:'instant'})", element)
                wait.until(lambda _: js("const r=arguments[0].getBoundingClientRect();return r.top>=0 && r.bottom<=innerHeight+1 && arguments[0].contains(document.elementFromPoint(r.left+r.width/2,r.top+r.height/2))", element))
                fits(element)

            def expanded_panels():
                if "rail-collapsed" in find("body").get_attribute("class"):
                    find("[data-rail-toggle]").click()
                if "assistant-collapsed" in find("body").get_attribute("class"):
                    find("[data-assistant-expand]").click()

            def disclosure_links():
                disclosure = find(".matter-section-disclosure")
                assert disclosure.is_displayed()
                summary = disclosure.find_element(By.CSS_SELECTOR, "summary")
                fits(summary)
                assert summary.text.strip(), "The current section has no visible label"
                if not disclosure.get_attribute("open"):
                    reachable(summary)
                    summary.click()
                links = disclosure.find_elements(By.CSS_SELECTOR, "a")
                assert len(links) == 6
                assert len({link.get_attribute("href") for link in links}) == 6
                assert len([link for link in links if link.get_attribute("aria-current") == "page"]) == 1
                for link in links:
                    assert link.is_displayed()
                    fits(link, disclosure)
                    # The label must wrap or fit, never be silently clipped.
                    assert js("const range=document.createRange();range.selectNodeContents(arguments[0]);const a=arguments[0].getBoundingClientRect();return [...range.getClientRects()].every(r=>r.left>=a.left-1&&r.right<=a.right+1)", link)
                return disclosure, summary, links

            def section_navigation():
                """Six destinations: direct links when they fit, else the navigator."""
                if driver.find_elements(By.CSS_SELECTOR, ".matter-section-disclosure"):
                    return disclosure_links()[2]
                links = find("main .matter-section-tabs").find_elements(By.CSS_SELECTOR, ":scope > a")
                assert len(links) == 6
                assert len([link for link in links if link.get_attribute("aria-current") == "page"]) == 1
                for link in links:
                    assert link.is_displayed()
                    fits(link)
                    assert js("const range=document.createRange();range.selectNodeContents(arguments[0]);const a=arguments[0].getBoundingClientRect();return [...range.getClientRects()].every(r=>r.left>=a.left-1&&r.right<=a.right+1)", link)
                return links

            viewport(1024, 768)
            open_page(paths["notes"])
            expanded_panels()
            tools, library = find(".notebook-tools"), find(".notebook-library")
            wait.until(lambda _: rect(library)["top"] >= rect(tools)["bottom"] - 1)
            assert rect(library)["width"] >= 380
            for element in (library, find(".notebook-stats"), find(".notebook-filter-form")):
                fits(element, find(".notebook-shell"))
            assert js("return arguments[0].clientHeight >= arguments[0].scrollHeight - 1", tools)
            if driver.find_elements(By.CSS_SELECTOR, ".matter-section-disclosure"):
                disclosure_links()[1].click()
            else:
                section_navigation()
            no_page_overflow()
            measurements["constrained_notes"] = {"tools": rect(tools), "library": rect(library), "panels": find("body").get_attribute("class")}
            screenshot("notes-constrained-with-assistant")
            checks.append("1024x768 with both panels open stacks complete tools above usable notes, filters and statistics; current navigation fits")

            # Resize and toggle the live page rather than relying on fresh-load breakpoints.
            viewport(1800, 1000)
            find("[data-assistant-collapse]").click()
            wait.until(lambda _: js("return !document.querySelector('.matter-section-disclosure')"))
            assert all(link.is_displayed() for link in find("main .matter-section-tabs").find_elements(By.CSS_SELECTOR, "a"))
            viewport(940, 768)
            find("[data-assistant-expand]").click()
            wait.until(lambda _: find(".matter-section-disclosure").is_displayed())
            disclosure_links()[1].click()
            viewport(1800, 1000)
            wait.until(lambda _: js("return !document.querySelector('.matter-section-disclosure')"))
            fits(find("main .matter-section-tabs"))
            checks.append("Navigation changes between direct links and a section disclosure after live resizing and Assistant toggles")

            # A compact navigation action must not cover destinations or destroy
            # an in-progress question. Resizing must preserve visible focus.
            viewport(1024, 768)
            open_page(paths["notes"])
            expanded_panels()
            preference = f"{LEGACY_STEM}:assistant:display:v2"
            js("localStorage.setItem(arguments[0], 'open')", preference + ":compact")
            question = find("#assistant-question")
            question.send_keys("Synthetic unsent question")
            picker = find("[data-assistant-conversation-picker]")
            picker.send_keys(Keys.NULL)
            viewport(820, 768)
            wait.until(lambda _: driver.switch_to.active_element == picker and picker.is_displayed())
            viewport(1024, 768)
            wait.until(lambda _: driver.switch_to.active_element == picker and picker.is_displayed())
            viewport(390, 844)
            # The page builds the disclosure from resize callbacks after the resize returns.
            wait.until(lambda _: driver.find_elements(By.CSS_SELECTOR, ".matter-section-disclosure summary"))
            summary = find(".matter-section-disclosure summary")
            reachable(summary)
            summary.click()
            wait.until(lambda _: "assistant-collapsed" in find("body").get_attribute("class"))
            assert js("return localStorage.getItem(arguments[0])", preference) == "open"
            find("[data-assistant-expand]").click()
            assert find("#assistant-question").get_attribute("value") == "Synthetic unsent question"
            find("[data-assistant-collapse]").click()
            wait.until(lambda _: find("[data-assistant-expand]").is_displayed())
            js("arguments[0].disabled=true", question)
            viewport(1024, 768)
            wait.until(lambda _: "assistant-collapsed" not in find("body").get_attribute("class")
                and question.is_displayed()
                and driver.switch_to.active_element == find("[data-assistant-collapse]"))
            js("arguments[0].disabled=false", question)
            reachable(question)
            question.clear()
            checks.append("Assistant preserves drafts and desktop preference when compact navigation opens; resizing preserves visible focus and provides an enabled fallback when the question field is disabled")

            # All destinations are actual working links. Keep query and fragment contracts.
            for width, height in ((390, 844), (320, 568)):
                viewport(width, height)
                open_page("/setup?view=list")
                disclosure, summary, links = disclosure_links()
                assert "Review" in summary.text
                destinations = [(link.text, link.get_attribute("href")) for link in links]
                if width == 390:
                    for label, destination in destinations:
                        open_page("/setup?view=list")
                        _, _, choices = disclosure_links()
                        link = next(link for link in choices if link.get_attribute("href") == destination)
                        reachable(link)
                        link.click()
                        wait.until(lambda _: urlsplit(driver.current_url).path == urlsplit(destination).path)
                        assert driver.find_elements(By.CSS_SELECTOR, "main"), label
                        assert "Internal Server Error" not in find("body").text, label
                        if urlsplit(destination).query:
                            assert urlsplit(driver.current_url).query == urlsplit(destination).query, (label, driver.current_url)
                        if urlsplit(destination).fragment:
                            assert urlsplit(driver.current_url).fragment == urlsplit(destination).fragment, (label, driver.current_url)
                open_page("/setup?view=list")
                disclosure = find(".matter-section-disclosure")
                summary = disclosure.find_element(By.CSS_SELECTOR, "summary")
                summary.send_keys(Keys.ENTER)
                links = disclosure.find_elements(By.CSS_SELECTOR, "a")
                for link in links:
                    driver.switch_to.active_element.send_keys(Keys.TAB)
                    assert driver.switch_to.active_element == link
                destination = links[-1].get_attribute("href")
                driver.switch_to.active_element.send_keys(Keys.ENTER)
                wait.until(lambda _: urlsplit(driver.current_url).path == urlsplit(destination).path)
                open_page("/setup?view=list")
                summary = find(".matter-section-disclosure summary")
                summary.send_keys(Keys.TAB)
                assert not js("return arguments[0].contains(document.activeElement)", find(".matter-section-disclosure")), "Closed links remain in tab order"
                for element in (find("[data-activity-toggle]"), find(".account-menu summary")):
                    fits(element, find(".topbar"))
                find(".account-menu summary").click()
                for element in (find(".account-menu-panel"), find("#account-appearance-theme"), find(".logout-form button")):
                    fits(element)
                    assert element.is_displayed()
                find(".account-menu summary").click()
                row = find(".source-table-row")
                name = row.find_element(By.CSS_SELECTOR, ".source-name-cell strong")
                assert name.text == SOURCE_NAME
                name_bounds = fits(name, row)
                assert name_bounds["width"] >= 140, name_bounds
                assert js("const r=document.createRange();r.selectNodeContents(arguments[0]);const b=arguments[1].getBoundingClientRect();return [...r.getClientRects()].every(a=>a.left>=b.left-1&&a.right<=b.right+1)", name, row)
                status = row.find_element(By.CSS_SELECTOR, ".source-review-cell")
                assert "unreviewed" in status.text.casefold()
                fits(status, row)
                selection = row.find_element(By.CSS_SELECTOR, "[data-source-select]")
                reachable(selection)
                selection.click()
                assert selection.is_selected()
                assert find("[data-source-selected-count]").text == "1"
                bulk_action = find("#source-bulk-action")
                assert bulk_action.accessible_name == "Bulk action"
                labels = js("return Array.from(arguments[0].labels || [])", bulk_action)
                assert labels and any(label.is_displayed() and rect(label)["width"] > 20
                    and rect(label)["height"] > 10 for label in labels), "Bulk action needs a visible associated label"
                Select(bulk_action).select_by_value("reviewed")
                assert find("[data-source-bulk-submit]").is_enabled()
                action = row.find_element(By.CSS_SELECTOR, ".source-open-action")
                reachable(action)
                no_page_overflow()
                measurements[f"mobile_source_{width}"] = {"row": rect(row), "filename": name_bounds, "status": rect(status)}
                screenshot(f"sources-mobile-actions-{width}")
                destination = action.get_attribute("href")
                action.click()
                wait.until(lambda _: urlsplit(driver.current_url).path == urlsplit(destination).path)
                wait.until(lambda _: SOURCE_TEXT in find("main").text)
            checks.append("320px and 390px provide current section, all nine navigable destinations, native keyboard traversal and no closed-menu tab stops")
            checks.append("Mobile Activity/account/Appearance/sign-out are visible; source filename, review status, selection, labelled bulk action and Read document work together")

            # At short desktop height, submit the action users reported losing and
            # follow the generated candidate all the way to its original support.
            viewport(1440, 480)
            go(paths["notes"])
            # The tools now belong to the document. End would start a smooth
            # scroll to the bottom of the entire notes collection, racing an
            # immediate scrollIntoView. Traverse to the action as a keyboard
            # reviewer does, then wait for its native focus scroll to settle.
            find(".suggestion-tool select").send_keys(Keys.TAB)
            suggestion = find(".suggestion-tool button")
            previous_suggestion_geometry = None

            def suggestion_ready(_):
                nonlocal previous_suggestion_geometry
                result = js("""const e=arguments[0],r=e.getBoundingClientRect();
                    const top=document.querySelector('.topbar').getBoundingClientRect().bottom;
                    return {focused:document.activeElement===e,top:r.top,bottom:r.bottom,
                        topbar_bottom:top,viewport_height:innerHeight,scroll_y:scrollY,
                        unobscured:e.contains(document.elementFromPoint(r.left+r.width/2,r.top+r.height/2))};""", suggestion)
                measurements["short_desktop_suggestion_click"] = result
                geometry = (result["top"], result["bottom"], result["scroll_y"])
                settled = (previous_suggestion_geometry is not None
                    and all(abs(current - previous) <= .5
                        for current, previous in zip(geometry, previous_suggestion_geometry)))
                previous_suggestion_geometry = geometry
                return (settled and result["focused"] and result["unobscured"]
                    and result["top"] >= result["topbar_bottom"] - 1
                    and result["bottom"] <= result["viewport_height"] + 1)

            wait.until(suggestion_ready)
            fits(suggestion)
            suggestion.click()
            wait.until(lambda _: driver.find_elements(By.CSS_SELECTOR, ".notebook-item.status-suggested .notebook-provenance a"))
            candidate = next(item for item in driver.find_elements(By.CSS_SELECTOR, ".notebook-item.status-suggested") if "North Annex" in item.text)
            assert "suggested" in candidate.text.casefold()
            support_link = candidate.find_element(By.CSS_SELECTOR, ".notebook-provenance a")
            reachable(support_link)
            support_link.click()
            wait.until(lambda _: find(".support-pane").is_displayed())
            assert "North Annex" in find(".support-pane").text
            screenshot("notes-suggestion-original-support")
            checks.append("At 1440x480, native Tab reaches Find review suggestions and settles visibly; native click produces a Suggested North Annex candidate and its source support opens")

            def keyboard_focus(element):
                reachable(element)
                element.send_keys(Keys.NULL)
                # Keys.NULL focuses without changing field contents; entering Tab
                # and Shift+Tab establishes keyboard modality for every control.
                element.send_keys(Keys.TAB)
                driver.switch_to.active_element.send_keys(Keys.SHIFT, Keys.TAB)
                assert driver.switch_to.active_element == element
                style = js("const s=getComputedStyle(arguments[0]);return {visible:arguments[0].matches(':focus-visible'),outline:s.outlineStyle,width:parseFloat(s.outlineWidth),color:s.outlineColor}", element)
                assert style["visible"] and style["outline"] not in ("none", "hidden") and style["width"] >= 2, style
                assert style["color"] not in ("transparent", "rgba(0, 0, 0, 0)"), style
                return style

            viewport(1440, 900)
            from browser_appearance import APPEARANCES
            for mode, accent in APPEARANCES:
                theme = f"{mode}-{accent}"
                open_page(paths["notes"])
                js("document.documentElement.dataset.theme=arguments[0];document.documentElement.dataset.accent=arguments[1]", mode, accent)
                driver.find_element(By.TAG_NAME, "body").send_keys(Keys.TAB)
                skip = find(".skip-link")
                assert driver.switch_to.active_element == skip
                assert rect(skip)["top"] >= 0
                skip.send_keys(Keys.ENTER)
                main = find("main")
                assert driver.switch_to.active_element == main
                driver.switch_to.active_element.send_keys(Keys.TAB)
                assert js("return arguments[0].contains(document.activeElement)", main)
                expanded_panels()
                controls = [find("#matter-filter"), find("#assistant-question"),
                    find('.notebook-item-form input[name="title"]'), find('.notebook-item-form select[name="item_type"]'),
                    find('.notebook-item-form button'), find('.notebook-export-menu summary'), find('.notebook-stats a')]
                measurements[f"focus_{theme}"] = [keyboard_focus(control) for control in controls]
                driver.execute_cdp_cmd("Emulation.setEmulatedMedia", {"features": [{"name": "forced-colors", "value": "active"}]})
                assert js("return matchMedia('(forced-colors: active)').matches")
                measurements[f"forced_colors_focus_{theme}"] = [keyboard_focus(control) for control in controls]
                driver.execute_cdp_cmd("Emulation.setEmulatedMedia", {"features": []})
                go(paths["review"])
                js("document.documentElement.dataset.theme=arguments[0];document.documentElement.dataset.accent=arguments[1]", mode, accent)
                question = find('.composer-wrap textarea[name="question"]')
                measurements[f"question_focus_{theme}"] = keyboard_focus(question)
            checks.append("All six mode/accent combinations skip links activate actual main content; keyboard links, summaries, inputs, selects and buttons have opaque outlines including forced-colors")

            # Font preference and WCAG-style spacing are separate stress cases.
            # Neither this nor a CSS viewport change is evidence of native zoom.
            for treatment in ("root-font-200-percent", "text-spacing"):
                viewport(1024, 768)
                open_page(paths["notes"])
                expanded_panels()
                if treatment == "root-font-200-percent":
                    js("document.documentElement.style.fontSize='200%'")
                    assert js("return parseFloat(getComputedStyle(document.documentElement).fontSize)") >= 32
                else:
                    js("const s=document.createElement('style');s.textContent='*{line-height:1.5!important;letter-spacing:.12em!important;word-spacing:.16em!important}p{margin-bottom:2em!important}';document.head.append(s)")
                no_page_overflow()
                for selector in (".notebook-tools", ".notebook-library", ".notebook-stats", ".notebook-filter-form"):
                    fits(find(selector), find(".notebook-shell"))
                button = find(".suggestion-tool button")
                reachable(button)
                assert button.is_enabled()
                section_navigation()
                screenshot(treatment)
            checks.append("At 1024px with panels open, independent 200% root-font and expanded text-spacing stress cases retain reachable suggestion controls and navigation without horizontal clipping")

            viewport(3840, 2160)
            go(paths["notes"])
            prose = find(".notebook-body")
            bounds = fits(prose)
            ch = js("const s=document.createElement('span');s.style.cssText='position:absolute;width:1ch';s.style.font=getComputedStyle(arguments[0]).font;document.body.append(s);const w=s.getBoundingClientRect().width;s.remove();return w", prose)
            assert 50 <= bounds["width"] / ch <= 90, (bounds["width"], ch)
            article = find(".notebook-item")
            assert rect(article)["width"] <= 1400, rect(article)
            assert abs(rect(find(".notebook-heading"))["left"] - rect(find("main .matter-section-tabs"))["left"]) <= 1
            actions = article.find_element(By.CSS_SELECTOR, ".notebook-item-actions")
            assert rect(actions)["right"] <= bounds["right"] + 100
            measurements["wide_notes"] = {"prose": bounds, "characters_approx": bounds["width"] / ch, "card": rect(article)}
            screenshot("notes-balanced-4k")
            checks.append("At 3840x2160, notes maintain a bounded reading measure and nearby actions without turning the whole workspace into a narrow column")
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
