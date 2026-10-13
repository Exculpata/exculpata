#!/usr/bin/env python3
"""Exercise saved Report downloads and deliberate synthetic closure in Chrome."""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta
import io
import json
import socket
import sys
import tempfile
import threading
import zipfile
from pathlib import Path
from urllib.parse import urlparse

import uvicorn
from selenium import webdriver
from selenium.common.exceptions import WebDriverException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import Select, WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from case_intelligence.generation import UnavailableGenerator  # noqa: E402
from case_intelligence.identity import PREVIEW_IDENTITIES, SESSION_COOKIE  # noqa: E402
from case_intelligence.matter_archive_verifier import verify_matter_archive  # noqa: E402
from case_intelligence.workbench import create_workbench_app  # noqa: E402

PREVIEW = next(identity for identity in PREVIEW_IDENTITIES if identity.subject == "taylor-morgan")
ACTOR = PREVIEW.preferred_principal_id


def completed_download(directory: Path, before: set[Path], suffix: str) -> Path | None:
    paths = list(directory.iterdir())
    if any(path.suffix == ".crdownload" for path in paths):
        return None
    for path in paths:
        if path in before or path.suffix != suffix:
            continue
        try:
            if not path.is_file() or path.stat().st_size == 0:
                continue
            if suffix in {".zip", ".docx"} and not zipfile.is_zipfile(path):
                continue
        except OSError:
            continue  # Chrome may still be renaming its temporary download.
        return path
    return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--chrome-binary", type=Path, required=True)
    parser.add_argument("--chromedriver", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--verify-readiness", action="store_true")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    checks = []
    def record_check(message):
        checks.append(message)
        print(message, flush=True)

    with tempfile.TemporaryDirectory(prefix="recordbench-reports-browser-") as temporary:
        root = Path(temporary).resolve()
        downloads = root / "downloads"
        downloads.mkdir()
        original = root / "generated-note.txt"
        original.write_bytes(b"Synthetic source: the blue vehicle arrived at noon.\n")
        original_bytes = original.read_bytes()
        app = create_workbench_app(
            root / "runtime", generator=UnavailableGenerator(), auth_mode="preview",
            background_ingestion=True,
        )
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        base = f"http://127.0.0.1:{listener.getsockname()[1]}"
        server = uvicorn.Server(uvicorn.Config(app, log_level="warning", access_log=False))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()
        options = Options()
        options.binary_location = str(args.chrome_binary)
        for flag in ("--headless=new", "--no-sandbox", "--disable-dev-shm-usage", "--window-size=1440,1000"):
            options.add_argument(flag)
        options.add_experimental_option("prefs", {
            "download.default_directory": str(downloads), "download.prompt_for_download": False,
        })
        driver = None
        try:
            driver = webdriver.Chrome(service=Service(str(args.chromedriver)), options=options)
            driver.set_page_load_timeout(30)
            driver.set_script_timeout(30)
            driver.execute_cdp_cmd("Emulation.setTimezoneOverride", {"timezoneId": "America/New_York"})
            driver.implicitly_wait(3)
            wait = WebDriverWait(driver, 20)
            wait.until(lambda _: server.started)

            def go(path):
                driver.get(base + path)

            def detached(element):
                try:
                    return EC.staleness_of(element)(driver)
                except WebDriverException as exc:
                    if "Node with given id does not belong to the document" not in exc.msg:
                        raise
                    return True

            def click(selector):
                element = driver.find_element(By.CSS_SELECTOR, selector)
                driver.execute_script("arguments[0].scrollIntoView({block:'center',behavior:'instant'});", element)
                submits = element.tag_name == "button" and element.get_attribute("type") == "submit"
                element.click()
                if submits:
                    wait.until(lambda _: detached(element))

            def fill(selector, value):
                element = driver.find_element(By.CSS_SELECTOR, selector)
                element.clear()
                element.send_keys(value)

            def edit_report():
                # Report mutations return to the reading view. Enter the
                # visible editor again before interacting with its controls.
                click(".report-reading-page a[href*='edit=true']")
                wait.until(EC.visibility_of_element_located((By.CSS_SELECTOR, ".report-settings")))

            def download(selector, suffix):
                before = set(downloads.iterdir())
                click(selector)
                return wait.until(lambda _: completed_download(downloads, before, suffix))

            def login(subject=PREVIEW.subject):
                driver.delete_all_cookies()
                go("/auth/login?next=/matters/manage")
                click(f'button[name="identity_subject"][value="{subject}"]')
                wait.until(lambda d: "/auth/login" not in d.current_url)

            def capture_closure(name, focus=None):
                for width in (1440, 430):
                    driver.set_window_size(width, 1000)
                    settle_layout()
                    if width < 901:
                        rail = driver.find_elements(By.CSS_SELECTOR, "[data-rail-toggle]")
                        if rail and rail[0].get_attribute("aria-expanded") == "true":
                            click("[data-rail-toggle]")
                        wait.until(lambda d: d.execute_script(
                            "return document.querySelector('[data-matter-rail]').getBoundingClientRect().right <= 0 "
                            "&& getComputedStyle(document.querySelector('[data-rail-scrim]')).visibility === 'hidden'"))
                    wait.until(lambda d: d.execute_script(
                        "const box = document.querySelector('.close-matter-card').getBoundingClientRect(); "
                        "return box.left >= 0 && box.right <= innerWidth + 1 "
                        "&& document.documentElement.scrollWidth <= innerWidth + 1"))
                    if focus:
                        driver.execute_script(
                            "window.scrollTo({top:scrollY + document.querySelector(arguments[0]).getBoundingClientRect().top - 80,"
                            "left:0,behavior:'instant'});", focus)
                        wait.until(lambda d: d.execute_script(
                            "const box = document.querySelector(arguments[0]).getBoundingClientRect(); "
                            "return box.top >= 64 && box.bottom <= innerHeight;", focus))
                    else:
                        driver.execute_script("window.scrollTo({top:0,left:0,behavior:'instant'});")
                    driver.save_screenshot(str(args.output / f"{name}-{width}.png"))
                driver.set_window_size(1440, 1000)

            def fresh_confirmation(target):
                current = bench.workspace.case_lifecycle.current_exit(target.matter_id)
                eligible = datetime.fromisoformat(current.eligible_after.replace("Z", "+00:00"))
                # Only this temporary synthetic harness advances time. A new
                # real browser sign-in after the jump supplies a fresh session.
                now = max(bench.workspace.current_time(), eligible + timedelta(seconds=1))
                bench.workspace._clock = lambda: now
                app.state.identity._clock = lambda: now
                login()
                target_prefix = f"/matters/{target.slug}"
                go(target_prefix + "/close")
                click(f'form[action="{target_prefix}/close/confirm"] button')
                wait.until(EC.visibility_of_element_located((By.ID, "confirmed-name")))
                return current

            def capture_notice(name):
                for width in (1440, 430):
                    driver.set_window_size(width, 1000)
                    settle_layout()
                    if width < 901 and driver.find_element(By.CSS_SELECTOR, "[data-rail-toggle]").get_attribute("aria-expanded") == "true":
                        click("[data-rail-toggle]")
                    if width < 901:
                        wait.until(lambda d: d.execute_script(
                            "return document.querySelector('[data-matter-rail]').getBoundingClientRect().right <= 0 "
                            "&& getComputedStyle(document.querySelector('[data-rail-scrim]')).visibility === 'hidden'"))
                    wait.until(lambda d: d.execute_script(
                        "const box = document.querySelector('[data-closure-notices]').getBoundingClientRect(); "
                        "return box.left >= 0 && box.right <= innerWidth + 1 "
                        "&& document.documentElement.scrollWidth <= innerWidth + 1"))
                    driver.execute_script("window.scrollTo({top:0,left:0,behavior:'instant'});")
                    driver.save_screenshot(str(args.output / f"{name}-{width}.png"))
                driver.set_window_size(1440, 1000)

            def settle_layout():
                wait.until(lambda d: d.execute_script(
                    "const elements = [...document.querySelectorAll('[data-matter-rail],.app-frame')]; "
                    "elements.forEach(element => element.getBoundingClientRect()); "
                    "return elements.every(element => element.getAnimations().every(animation => animation.playState !== 'running')) "
                    "&& document.documentElement.scrollWidth <= innerWidth + 1"))

            def prepare_exit(target):
                target_prefix = f"/matters/{target.slug}"
                go(target_prefix + "/close")
                click(f'form[action="{target_prefix}/close/begin"] button')
                assert bench.workspace.matter_lifecycle(target.matter_id).state == "closing"
                assert not driver.find_elements(By.ID, "confirmed-name")
                click(f'form[action="{target_prefix}/close/export"] button')
                current = bench.workspace.case_lifecycle.current_exit(target.matter_id)
                assert current.export_id, driver.find_element(By.TAG_NAME, "body").text
                archive = download(f'a[href="{target_prefix}/close/export"]', ".zip")
                manifest = verify_matter_archive(archive, expected_sha256=current.archive_digest)
                assert manifest["exit_id"] == current.exit_id and manifest["export_id"] == current.export_id
                assert manifest["original_source_files_included"] is True
                click(f'.close-explanation a[href="{target_prefix}/close"]')
                assert "observed delivery of the complete archive to your account" in driver.find_element(By.TAG_NAME, "body").text
                fresh_confirmation(target)
                return archive, manifest, current

            def confirm_deletion(target):
                fill("#confirmed-name", target.display_name)
                click("input[name=export_acknowledged]")
                click("input[name=acknowledge]")
                click(".close-matter-form button[type=submit]")

            login()
            go("/matters/new")
            fill("#matter-name", "Synthetic report acceptance")
            click(".matter-form button[type=submit]")
            wait.until(lambda d: "/setup" in d.current_url)
            slug = urlparse(driver.current_url).path.split("/")[2]
            prefix = f"/matters/{slug}"
            click("[data-assistant-collapse]")
            # Real selection, preflight, and confirmed browser upload.
            fill("[data-upload-collection-name]", "Synthetic report sources")
            driver.find_element(By.ID, "source-files").send_keys(str(original))
            wait.until(lambda d: d.find_element(By.CSS_SELECTOR, "[data-upload-preflight-confirm]").is_enabled())
            click("[data-upload-preflight-confirm]")
            bench = app.state.workbench
            matter = bench.matter(slug, ACTOR)
            document = wait.until(lambda _: next((item for item in bench.source_store(matter).documents.values()
                                                  if item.state == "ready"), None))
            candidate = bench._candidate(matter, document, document.parsed_units()[0], 1)
            token = bench._support_token(candidate)
            go(prefix + f"?support={token}#support-pane")
            assert "the blue vehicle arrived at noon" in driver.find_element(By.ID, "support-pane").text
            click(".support-save-button")
            wait.until(lambda d: "/notebook" in d.current_url)
            record_check("Upload, exact source passage, and save to case notes")

            go(prefix + "/reports")
            click(".report-create-card summary")
            fill(".report-create-card input[name=title]", "Synthetic review memo")
            fill(".report-create-card textarea[name=purpose]", "Preserve edited source-supported work.")
            click(".report-create-card button[type=submit]")
            edit_report()
            click(".report-add-material details:nth-child(2) summary")
            click("form[action*='/from-notebook/'] button")
            edit_report()
            click(".report-add-material details:first-child summary")
            fill(".report-add-material input[name=heading]", "Human conclusion")
            fill(".report-add-material textarea[name=body]", "Original draft text.")
            click(".report-add-material details:first-child button")
            edit_report()
            cards = driver.find_elements(By.CSS_SELECTOR, ".report-section-card")
            assert len(cards) == 2
            fill(f"#{cards[1].get_attribute('id')} textarea[name=body]", "Edited conclusion retained in both exports.")
            click(f"#{cards[1].get_attribute('id')} .report-section-content form:first-child button")
            edit_report()
            move = driver.find_element(By.CSS_SELECTOR, "button[aria-label='Move Human conclusion up']")
            move.send_keys(Keys.ENTER)
            wait.until(lambda _: detached(move))
            edit_report()
            assert driver.find_elements(By.CSS_SELECTOR, ".report-section-card input[name=heading]")[0].get_attribute("value") == "Human conclusion"
            Select(driver.find_element(By.CSS_SELECTOR, ".report-settings select")).select_by_value("final")
            click(".report-settings form:first-child button")
            record_check("Create, edit, reorder by keyboard, and finalize Report")

            individual_md = download(".report-export-actions a[href$='markdown']", ".md").read_text()
            individual_docx = download(".report-export-actions a[href$='docx']", ".docx").read_bytes()
            click(".report-reading-sources a")
            assert "the blue vehicle arrived at noon" in driver.find_element(By.ID, "support-pane").text
            record_check("Individual Markdown/Word download and Report citation opens exact source")

            source_support_expected = True
            def check_bundle(path):
                with zipfile.ZipFile(path) as archive:
                    manifest = json.loads(archive.read("manifest.json"))
                    assert manifest["report_count"] == 1
                    item = manifest["reports"][0]
                    assert item["status"] == "final" and item["section_count"] == (2 if source_support_expected else 1)
                    markdown = archive.read(item["markdown"]).decode()
                    normalize = lambda text: [line for line in text.splitlines()
                                             if "work product exported from" not in line]
                    assert normalize(markdown) == normalize(individual_md)
                    with zipfile.ZipFile(io.BytesIO(archive.read(item["docx"]))) as word:
                        xml = word.read("word/document.xml").decode()
                    assert "Edited conclusion retained in both exports." in xml
                    if source_support_expected:
                        assert "generated-note.txt" in xml and "Line 1" in xml
                        assert markdown.index("Human conclusion") < markdown.index("the blue vehicle")
                    else:
                        assert "generated-note.txt" not in xml
                    assert not manifest["original_source_files_included"]

            go(prefix + "/settings")
            bundle = download(f"a[href='{prefix}/export']", ".zip")
            check_bundle(bundle)
            record_check("Ordinary complete bundle inventory and reopened Markdown/Word match individual Report")
            go(prefix + "/reports")
            driver.save_screenshot(str(args.output / "reports-desktop.png"))
            driver.set_window_size(390, 844)
            if driver.find_element(By.CSS_SELECTOR, "[data-rail-toggle]").get_attribute("aria-expanded") == "true":
                click("[data-rail-toggle]")
            wait.until(lambda d: d.execute_script(
                "return document.querySelector('[data-matter-rail]').getBoundingClientRect().right <= 1;"
            ))
            driver.execute_script("window.scrollTo({top:0,left:0,behavior:'instant'});")
            driver.save_screenshot(str(args.output / "reports-mobile.png"))
            # Exercise the same reachable download at a narrow viewport.
            assert download(".report-export-actions a[href$='markdown']", ".md").read_text()
            record_check("Mobile Report download")

            # A bad saved citation must show recovery and produce no new ZIP.
            original_support = bench._assert_current_report_section_citations
            def unavailable(*_args, **_kwargs):
                raise KeyError("synthetic-unavailable-source")
            bench._assert_current_report_section_citations = unavailable
            go(prefix + "/settings")
            before = set(downloads.glob("*.zip"))
            click(f"a[href='{prefix}/export']")
            wait.until(lambda d: "no longer resolves" in d.find_element(By.TAG_NAME, "body").text)
            assert "Open the Report" in driver.find_element(By.TAG_NAME, "body").text
            assert set(downloads.glob("*.zip")) == before
            bench._assert_current_report_section_citations = original_support
            record_check("Visible source failure with no misleading complete download")

            if args.verify_readiness:
                driver.set_window_size(1440, 1000)
                before = set(downloads.iterdir())
                report = bench.workspace.reports(matter.matter_id, ACTOR)[0]
                before_revision = report.updated_at
                go(prefix + "/work-product")
                click(f'a[href="{prefix}/export-readiness"]')
                assert "Ready to download" in driver.find_element(By.TAG_NAME, "body").text
                assert "Checked at" in driver.find_element(By.TAG_NAME, "body").text
                assert set(downloads.iterdir()) == before
                assert bench.workspace.report(matter.matter_id, report.report_id).updated_at == before_revision
                for width in (1440, 430):
                    driver.set_window_size(width, 1000)
                    if width < 901:
                        wait.until(lambda d: d.execute_script("return document.querySelector('[data-matter-rail]').getBoundingClientRect().right <= 1"))
                    assert driver.execute_script("return document.documentElement.scrollWidth <= innerWidth + 2")
                    driver.save_screenshot(str(args.output / f"export-ready-{width}.png"))
                record_check("Deliberate export check shows its inspection time at desktop and narrow widths without a download or Report edit")
                driver.set_window_size(1440, 1000)

                go(prefix + "/setup?view=list")
                click(".source-table-row .remove-action")
                assert document.document_id not in bench.source_store(matter).documents
                go(prefix + "/work-product")
                click(f'a[href="{prefix}/export-readiness"]')
                assert "Export needs attention" in driver.find_element(By.TAG_NAME, "body").text
                assert "Remaining bundle checks did not finish" in driver.find_element(By.TAG_NAME, "body").text
                assert "Synthetic review memo" in driver.find_element(By.CSS_SELECTOR, ".close-matter-card li a").text
                driver.save_screenshot(str(args.output / "export-needs-attention.png"))
                click(".close-matter-card li a")
                assert "This Report needs attention before export" in driver.find_element(By.TAG_NAME, "body").text
                assert report.report_id in driver.current_url
                edit_report()
                Select(driver.find_element(By.CSS_SELECTOR, ".report-settings select")).select_by_value("draft")
                click(".report-settings form:first-child button")
                edit_report()
                cited_card = driver.find_element(By.CSS_SELECTOR, ".report-citations").find_element(By.XPATH, "ancestor::article")
                remove = cited_card.find_element(By.CSS_SELECTOR, 'form[action$="/delete"] button')
                driver.execute_script("arguments[0].scrollIntoView({block:'center',behavior:'instant'});", remove)
                remove.click()
                wait.until(EC.alert_is_present()).accept()
                wait.until(lambda _: detached(remove))
                edit_report()
                assert len(driver.find_elements(By.CSS_SELECTOR, ".report-section-card")) == 1
                assert driver.find_element(By.CSS_SELECTOR, ".report-section-card textarea").get_attribute("value") == "Edited conclusion retained in both exports."
                Select(driver.find_element(By.CSS_SELECTOR, ".report-settings select")).select_by_value("final")
                click(".report-settings form:first-child button")
                source_support_expected = False
                individual_md = download(".report-export-actions a[href$='markdown']", ".md").read_text()
                individual_docx = download(".report-export-actions a[href$='docx']", ".docx").read_bytes()
                record_check("A newly removed source blocks readiness and the repair link opens the affected Report; deliberate section removal preserves the human conclusion")

                go(prefix + "/work-product")
                click(f'a[href="{prefix}/export-readiness"]')
                assert "Ready to download" in driver.find_element(By.TAG_NAME, "body").text
                checked_bundle = download(f'a[href="{prefix}/export"]', ".zip")
                check_bundle(checked_bundle)
                record_check("Rechecking after repair passes and the actual download contains the current final Report")

                other = "generated-export-foreign-owner"
                bench.workspace.upsert_principal("test", other, "Generated foreign owner", other, preferred_principal_id=other)
                foreign = bench.create_matter("Generated foreign preview boundary", "", other)
                bench.workspace.create_report(foreign.matter_id, other, "Foreign preview Report canary")
                go(f"/matters/{foreign.slug}/export-readiness")
                assert "Foreign preview Report canary" not in driver.find_element(By.TAG_NAME, "body").text
                assert "Matter not found" in driver.find_element(By.TAG_NAME, "body").text
                record_check("A foreign owner's export preview remains inaccessible")

                # Generated recovery fixture: valid cited work, then an injected
                # unavailable derived-store condition during ordinary UI closure.
                recovery = bench.create_matter("Generated interrupted close", "", ACTOR)
                recovery_prefix = f"/matters/{recovery.slug}"
                recovery_document, _ = bench.source_store(recovery).store_stream(
                    "generated-recovery.txt", "text/plain", io.BytesIO(b"Generated recovery source passage."))
                recovery_unit = recovery_document.parsed_units()[0]
                recovery_candidate = bench._candidate(recovery, recovery_document, recovery_unit, 1)
                recovery_report = bench.workspace.create_report(recovery.matter_id, ACTOR, "Generated recovery Report")
                bench.workspace.add_report_section(recovery.matter_id, recovery_report.report_id, ACTOR,
                    expected_status="draft", heading="Generated supported finding", body="Preserve generated review work.",
                    citations=({"kind": "source", "document_id": recovery_document.document_id,
                        "source_version_id": recovery_document.version_id, "source_name": recovery_document.display_name,
                        "location": recovery_candidate.citation, "support_token": bench._support_token(recovery_candidate),
                        "excerpt": recovery_unit.text},))
                recovery_archive, recovery_manifest, recovery_exit = prepare_exit(recovery)
                assert recovery_manifest["table_rows"]["workbench_report"] == 1
                with zipfile.ZipFile(recovery_archive) as archive:
                    assert archive.read("originals/" + recovery_document.document_id) == b"Generated recovery source passage."
                prior_projection = bench._postgres_projection_configured, bench.postgres_connection, bench.postgres_ready
                try:
                    bench._postgres_projection_configured, bench.postgres_connection, bench.postgres_ready = True, None, False
                    confirm_deletion(recovery)
                    wait.until(lambda _: bench.workspace.matter_lifecycle(recovery.matter_id).state == "purge_failed")
                finally:
                    bench._postgres_projection_configured, bench.postgres_connection, bench.postgres_ready = prior_projection
                go(recovery_prefix + "/export-readiness")
                body = driver.find_element(By.TAG_NAME, "body").text
                assert "Export needs attention" in body and recovery_report.title in body
                assert "Reports cannot be edited" in body
                assert not driver.find_elements(By.CSS_SELECTOR, f'a[href^="{recovery_prefix}/reports"]')
                for width in (1440, 430):
                    driver.set_window_size(width, 1000)
                    if width < 901:
                        wait.until(lambda d: d.execute_script("return document.querySelector('[data-matter-rail]').getBoundingClientRect().right <= 1"))
                    wait.until(lambda d: d.execute_script("const box = document.querySelector('.close-matter-card').getBoundingClientRect(); return box.left >= 0 && box.right <= innerWidth + 1 && document.documentElement.scrollWidth <= innerWidth + 1"))
                    driver.execute_async_script("const done = arguments[arguments.length - 1]; requestAnimationFrame(() => requestAnimationFrame(done));")
                    driver.save_screenshot(str(args.output / f"readiness-failed-close-{width}.png"))
                click(f'a[href="{recovery_prefix}/close"]')
                assert driver.find_element(By.ID, "close-matter-heading").is_displayed()
                assert "fresh confirmation is required" in driver.find_element(By.TAG_NAME, "body").text
                retried = fresh_confirmation(recovery)
                assert retried.export_id == recovery_exit.export_id
                assert retried.archive_digest == recovery_exit.archive_digest
                assert not driver.find_elements(By.CSS_SELECTOR, f'form[action="{recovery_prefix}/close/export"]')
                confirm_deletion(recovery)
                wait.until(lambda _: bench.workspace.matter_lifecycle(recovery.matter_id).state == "deleted")
                record_check("Failed-close readiness retains Report details without unavailable edit links; Close matter recovery opens and deliberate retry completes")

            # Synthetic legacy matter: viewing the invitation must not enroll it.
            with bench.workspace._lock, bench.workspace.connection:
                bench.workspace.connection.execute(
                    "UPDATE workbench_case_policy SET mode='unscheduled',next_check_in_at=NULL WHERE matter_id=?",
                    (matter.matter_id,))
            legacy_policy = bench.workspace.case_lifecycle.policy(matter.matter_id)
            go('/matters/manage')
            assert driver.find_element(By.CSS_SELECTOR, '[data-check-in-invitation]').is_displayed()
            go(prefix + "/settings")
            invitation = driver.find_element(By.CSS_SELECTOR, '[data-check-in-invitation]')
            assert 'Nothing changes until' in invitation.text
            assert bench.workspace.case_lifecycle.policy(matter.matter_id) == legacy_policy
            for width in (1440, 430):
                driver.set_window_size(width, 1000)
                settle_layout()
                driver.execute_script("arguments[0].scrollIntoView({block:'center'});", invitation)
                wait.until(lambda d: d.execute_script(
                    "return document.documentElement.scrollWidth <= innerWidth + 1"))
                driver.save_screenshot(str(args.output / f"legacy-check-in-invitation-{width}.png"))
            driver.set_window_size(1440, 1000)
            click(f'form[action="{prefix}/check-in"] button')
            assert not driver.find_elements(By.CSS_SELECTOR, '[data-check-in-invitation]')
            record_check("An unscheduled legacy matter stays unchanged while its invitation is viewed; explicit owner enrollment alone starts 90-day check-ins")
            policy = bench.workspace.case_lifecycle.policy(matter.matter_id)
            assert policy.mode == "ongoing" and policy.selected_by == ACTOR
            assert datetime.fromisoformat(policy.next_check_in_at.replace("Z", "+00:00")) >= bench.workspace.current_time() + timedelta(days=89)
            check_in_time = driver.find_element(By.CSS_SELECTOR, ".check-in-time")
            assert "UTC" in check_in_time.text and "Your time:" in check_in_time.text
            assert "America/New_York" in check_in_time.text
            assert check_in_time.find_element(By.TAG_NAME, "time").get_attribute("datetime") == policy.next_check_in_at
            member = next(identity for identity in PREVIEW_IDENTITIES if identity.subject == "jordan-lee")
            bench.workspace.add_member(matter.matter_id, member.preferred_principal_id, ACTOR)
            go(prefix + "/close")
            click(f'form[action="{prefix}/close/begin"] button')
            cancelled_exit = bench.workspace.case_lifecycle.current_exit(matter.matter_id)
            assert bench.workspace.matter_lifecycle(matter.matter_id).state == "closing"
            notice = driver.find_element(By.CSS_SELECTOR, "[data-closure-notices]")
            assert matter.display_name in notice.text and "cancellation window is open" in notice.text
            assert "America/New_York" in notice.text and "UTC" in notice.text
            capture_closure("closure-cancellation-window")
            login(member.subject)
            go("/matters/new")
            notice = driver.find_element(By.CSS_SELECTOR, "[data-closure-notices]")
            assert matter.display_name in notice.text and "Contact the matter owner" in notice.text
            assert not notice.find_elements(By.TAG_NAME, "a")
            capture_notice("closure-member-notice")
            events = [event for event in bench.workspace.audit_events(matter.matter_id)
                      if event.action == "matter.exit_notice" and event.actor_principal_id == member.preferred_principal_id]
            assert len(events) == 1 and events[0].details == {"role": "member"}
            login()
            go(prefix + "/close")
            click(f'form[action="{prefix}/close/cancel"] button')
            assert bench.workspace.matter_lifecycle(matter.matter_id).state == "active"
            assert bench.workspace.case_lifecycle.current_exit(matter.matter_id) is None
            assert bench.workspace.reports(matter.matter_id, ACTOR)[0].status == "final"
            assert not driver.find_elements(By.CSS_SELECTOR, "[data-closure-notices]")
            record_check("Owners and members see the cancellation banner across pages, with role-only presentation audit, UTC and viewer time, and no narrow-screen overflow; member receives no closure controls")
            record_check("Owner check-in selects 90 days; cancellation reopens with saved final Report intact")

            final_archive, final_manifest, final_exit = prepare_exit(matter)
            assert final_exit.exit_id != cancelled_exit.exit_id
            assert final_exit.eligible_after >= cancelled_exit.eligible_after
            assert final_manifest["table_rows"]["workbench_report"] == 1
            assert final_manifest["table_rows"]["workbench_report_section"] == (2 if source_support_expected else 1)
            with zipfile.ZipFile(final_archive) as archive:
                sections = archive.read("records/workbench_report_section.jsonl").decode()
                assert "Edited conclusion retained in both exports." in sections
                if source_support_expected:
                    assert archive.read("originals/" + document.document_id) == original_bytes
                else:
                    assert document.document_id in archive.read("unavailable-references.jsonl").decode()
            capture_closure("closure-final-confirmation", focus=".close-matter-form")
            confirm_deletion(matter)
            wait.until(lambda _: bench.workspace.matter_lifecycle(matter.matter_id).state == "deleted")
            verify_matter_archive(final_archive, expected_sha256=final_exit.archive_digest)
            check_bundle(checked_bundle if args.verify_readiness else bundle)
            assert original.read_bytes() == original_bytes
            with zipfile.ZipFile(io.BytesIO(individual_docx)) as word:
                assert "Edited conclusion retained" in word.read("word/document.xml").decode()
            record_check("Full preservation archive passes offline SHA verification; fresh session, elapsed window, exact name and both acknowledgements precede deletion; retained downloads and external original survive")

            def browser_post(path, values):
                # Same-origin requests exercise server rejection even when
                # ordinary UI controls are absent or native validation blocks.
                return driver.execute_async_script(
                    "const done = arguments[2]; const body = new URLSearchParams(arguments[1]); "
                    "body.set('csrf_token', document.body.dataset.csrfToken); "
                    "fetch(arguments[0], {method:'POST', body}).then(async response => "
                    "done({status:response.status, url:response.url, body:await response.text()}))"
                    ".catch(error => done({error:String(error)}));", path, values)

            no_export = bench.create_matter("Synthetic intentional deletion", "", ACTOR)
            no_export_prefix = f"/matters/{no_export.slug}"
            removed_document, _ = bench.source_store(no_export).store_stream(
                "synthetic-delete.txt", "text/plain", io.BytesIO(b"Synthetic content intentionally discarded.\n"))
            no_export_original = bench.source_store(no_export).files / removed_document.stored_name
            bench.workspace.create_report(no_export.matter_id, ACTOR, "Synthetic discarded report")
            bench.workspace.add_member(no_export.matter_id, member.preferred_principal_id, ACTOR)
            wait.until(lambda _: not any(bench.matter_active_work_counts(no_export.matter_id).values()))
            login(member.subject)
            go("/matters/new")
            denied = browser_post(no_export_prefix + "/close/begin", {"mode": "delete_without_export"})
            assert denied["status"] == 404
            assert bench.workspace.matter_lifecycle(no_export.matter_id).state == "active"
            login()
            go(no_export_prefix + "/close")
            click('input[name="mode"][value="delete_without_export"]')
            click(f'form[action="{no_export_prefix}/close/begin"] button')
            no_export_exit = bench.workspace.case_lifecycle.current_exit(no_export.matter_id)
            assert no_export_exit.mode == "delete_without_export" and no_export_exit.export_id is None
            assert "No preservation archive or backup will be provided" in driver.find_element(By.TAG_NAME, "body").text
            assert not driver.find_elements(By.CSS_SELECTOR, f'a[href="{no_export_prefix}/close/export"]')
            assert not driver.find_elements(By.CSS_SELECTOR, f'form[action="{no_export_prefix}/close/confirm"]')
            early = browser_post(no_export_prefix + "/close/confirm", {"mode": "delete_without_export"})
            assert "seven-day" in early["body"]
            assert bench.workspace.matter_lifecycle(no_export.matter_id).state == "closing"
            capture_closure("delete-without-export-window")
            previous_session = app.state.identity.resolve(driver.get_cookie(SESSION_COOKIE)["value"]).session.session_id
            fresh_confirmation(no_export)
            current_session = app.state.identity.resolve(driver.get_cookie(SESSION_COOKIE)["value"]).session.session_id
            assert previous_session != current_session
            assert "No export or backup will be provided." in driver.find_element(By.TAG_NAME, "body").text
            assert not driver.find_elements(By.NAME, "export_acknowledged")
            fill("#confirmed-name", no_export.display_name)
            click("input[name=acknowledge]")
            # Native required-checkbox validation must keep the form in place.
            driver.find_element(By.CSS_SELECTOR, ".close-matter-form button[type=submit]").click()
            assert not driver.execute_script("return document.querySelector('[name=no_backup_acknowledged]').checkValidity()")
            assert bench.workspace.matter_lifecycle(no_export.matter_id).state == "closing"
            no_backup_values = driver.execute_script(
                "return Object.fromEntries(new FormData(document.querySelector('.close-matter-form')))")
            denied = browser_post(no_export_prefix + "/close", no_backup_values)
            assert "Explicitly acknowledge permanent deletion without an export or backup" in denied["body"]
            assert bench.workspace.matter_lifecycle(no_export.matter_id).state == "closing"
            assert no_export_original.read_bytes() == b"Synthetic content intentionally discarded.\n"
            login(member.subject)
            go("/matters/new")
            no_backup_values["no_backup_acknowledged"] = "yes"
            denied = browser_post(no_export_prefix + "/close", no_backup_values)
            assert denied["status"] == 404
            assert bench.workspace.matter_lifecycle(no_export.matter_id).state == "closing"
            fresh_confirmation(no_export)
            fill("#confirmed-name", no_export.display_name)
            click("input[name=no_backup_acknowledged]")
            click("input[name=acknowledge]")
            capture_closure("delete-without-export-final", focus=".close-matter-form")
            click(".close-matter-form button[type=submit]")
            wait.until(lambda _: bench.workspace.matter_lifecycle(no_export.matter_id).state == "deleted")
            assert not (bench.storage.matters / no_export.matter_id).exists()
            assert not (bench.storage.root / "preservation-exports" / no_export.matter_id).exists()
            assert bench.workspace.connection.execute("SELECT count(*) FROM workbench_report WHERE matter_id=?", (no_export.matter_id,)).fetchone()[0] == 0
            assert original.read_bytes() == original_bytes
            record_check("Delete without export requires owner authority, seven days, a fresh session, exact name and both irreversible/no-backup acknowledgements; missing acknowledgement and member requests are refused, then intentional deletion removes saved content without creating an archive")
            (args.output / "receipt.json").write_text(json.dumps({
                "synthetic_only": True, "passed": True, "checks": checks,
            }, indent=2) + "\n")
            print(json.dumps({"passed": True, "checks": len(checks)}))
        except Exception:
            if driver is not None:
                driver.save_screenshot(str(args.output / "failure.png"))
                (args.output / "failure.html").write_text(driver.page_source)
            raise
        finally:
            if driver is not None:
                driver.quit()
            server.should_exit = True
            thread.join(timeout=10)
            listener.close()


if __name__ == "__main__":
    main()
