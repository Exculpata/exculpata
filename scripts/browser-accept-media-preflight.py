#!/usr/bin/env python3
"""Synthetic browser acceptance for recording checks; localhost preview only."""

from __future__ import annotations

from case_intelligence.legacy_names import (
    LEGACY_STEM,
)

from browser_navigation import navigate_to

import argparse
import hashlib
import json
import socket
import subprocess
import sys
import tempfile
import threading
import wave
from pathlib import Path
from urllib.parse import urlparse
from unittest.mock import patch

import uvicorn
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT)]
from case_intelligence.media_preflight import inspect_recording
from tests.test_media_preflight import ACTOR, SPEECH, ObservedProcessor, app_for, silence


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--chrome-binary", type=Path, required=True)
    parser.add_argument("--chromedriver", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    checks = []
    with tempfile.TemporaryDirectory(prefix=f"{LEGACY_STEM}-preflight-browser-") as temporary:
        root = Path(temporary)
        quiet = root / "Generated quiet recording.wav"
        silence(quiet, 8)
        late = root / "Generated speech after silence.wav"
        with wave.open(str(SPEECH), "rb") as source:
            speech = source.readframes(source.getnframes())
        with wave.open(str(late), "wb") as output:
            output.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
            output.writeframes(b"\0\0" * 16000 * 30 + speech + b"\0\0" * 16000 * 5)
        video = root / "Generated video without audio.mp4"
        subprocess.run(["/usr/bin/ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                        "color=c=teal:s=320x240:d=3", "-an", "-c:v", "libx264",
                        "-pix_fmt", "yuv420p", str(video)], check=True)
        original_digests = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in (quiet, late, video)}
        processor = ObservedProcessor()
        app = app_for(root / "runtime", processor, background=True)
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        base = f"http://127.0.0.1:{listener.getsockname()[1]}"
        server = uvicorn.Server(uvicorn.Config(app, log_level="warning", access_log=False, ws="none"))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()
        options = Options()
        options.binary_location = str(args.chrome_binary)
        for flag in ("--headless=new", "--no-sandbox", "--disable-dev-shm-usage", "--window-size=1440,1000"):
            options.add_argument(flag)
        driver = webdriver.Chrome(service=Service(str(args.chromedriver)), options=options)
        driver.implicitly_wait(1)
        wait = WebDriverWait(driver, 25)
        try:
            wait.until(lambda _: server.started)

            def go(path):
                navigate_to(driver, base + path, diagnostics=args.output)

            def click(selector):
                element = driver.find_element(By.CSS_SELECTOR, selector)
                driver.execute_script("arguments[0].scrollIntoView({block:'center',behavior:'instant'});", element)
                submits = element.tag_name == "button" and element.get_attribute("type") == "submit"
                element.click()
                if submits:
                    wait.until(EC.staleness_of(element))

            go("/matters/new")
            driver.find_element(By.ID, "matter-name").send_keys("Synthetic recording checks")
            click(".matter-form button[type=submit]")
            wait.until(lambda d: "/setup" in d.current_url)
            slug = urlparse(driver.current_url).path.split("/")[2]
            prefix = f"/matters/{slug}"
            bench = app.state.workbench
            matter = bench.matter(slug, ACTOR)
            store = bench.source_store(matter)

            def browser_upload(path):
                go(prefix + "/setup")
                collapses = driver.find_elements(By.CSS_SELECTOR, "[data-assistant-collapse]")
                if collapses and collapses[0].is_displayed():
                    collapses[0].click()
                collection_name = driver.find_element(By.CSS_SELECTOR, "[data-upload-collection-name]")
                collection_name.clear()
                collection_name.send_keys("Synthetic media-preflight collection")
                driver.find_element(By.ID, "source-files").send_keys(str(path))
                wait.until(lambda d: d.find_element(By.CSS_SELECTOR, "[data-upload-preflight-confirm]").is_enabled())
                click("[data-upload-preflight-confirm]")
                document = wait.until(lambda _: next((d for d in store.documents.values() if d.display_name == path.name), None))
                wait.until(lambda _: document.state not in {"queued", "processing"})
                wait.until(lambda d: "0 processing" in d.find_element(By.CSS_SELECTOR, "[data-upload-progress]").text)
                token = store.action_token(document)
                go(prefix + f"/sources/{token}")
                wait.until(lambda d: d.find_elements(By.CSS_SELECTOR, "[data-recording-check]"))
                return document, token

            document, video_token = browser_upload(video)
            assert document.state == "playback_only"
            assert "Video without audio" in driver.find_element(By.CSS_SELECTOR, "[data-recording-check]").text
            wait.until(lambda d: d.execute_script("return document.querySelector('[data-media-player]').readyState >= 1"))
            player = driver.find_element(By.CSS_SELECTOR, "[data-media-player]")
            driver.execute_script("arguments[0].scrollIntoView({block:'center',behavior:'instant'});", player)
            player.click()
            driver.execute_script("document.querySelector('[data-media-player]').play().catch(e => window.playFailure=e.name)")
            wait.until(lambda d: d.execute_script("return document.querySelector('[data-media-player]').currentTime > 0.2"))
            assert processor.submissions == 0
            driver.save_screenshot(str(args.output / "no-audio-playback.png"))
            checks.append("Browser upload accepts video without audio; original video plays and no transcription is submitted")
            go(prefix + "/home")
            assert "Resolve first" not in driver.find_element(By.TAG_NAME, "body").text
            assert "1 playback only" in driver.find_element(By.TAG_NAME, "body").text
            driver.save_screenshot(str(args.output / "video-only-home.png"))
            go(prefix + f"/sources/{video_token}")
            click("[data-activity-toggle]")
            wait.until(lambda d: d.find_elements(By.CSS_SELECTOR, "[data-activity-content]"))
            assert driver.find_element(By.CSS_SELECTOR, "[data-activity-content]").get_attribute("data-attention-count") == "0"
            assert not driver.find_element(By.CSS_SELECTOR, "[data-activity-badge]").is_displayed()
            click("[data-activity-close]")
            checks.append("Video-only playback is terminal without a Matter Home resolve item, unresolved Activity item or attention badge")

            failed = inspect_recording(root / "absent.wav", "audio/wav")
            with patch("case_intelligence.media_evidence.inspect_recording", return_value=failed):
                document, quiet_token = browser_upload(quiet)
            assert "Recording check did not finish" in driver.find_element(By.CSS_SELECTOR, "[data-recording-check]").text
            assert not driver.find_elements(By.CSS_SELECTOR, "[data-recording-check] button[value=continue]")
            assert processor.submissions == 0
            click("[data-recording-check] button[value=retry]")
            wait.until(lambda d: "No speech detected" in d.find_element(By.CSS_SELECTOR, "[data-recording-check]").text)
            driver.refresh()
            assert "No speech detected" in driver.find_element(By.CSS_SELECTOR, "[data-recording-check]").text
            assert processor.submissions == 0
            checks.append("Failed inspection has a working retry; a no-speech decision survives navigation/reload without submission")

            go(prefix)
            wait.until(lambda d: d.find_elements(By.CSS_SELECTOR, "[data-media-activity-list] .media-activity-marker.attention"))
            panel = driver.find_element(By.CSS_SELECTOR, "[data-media-activity]")
            assert "Review recording" in panel.text
            assert "review the failure" not in panel.text
            assert not panel.find_elements(By.CSS_SELECTOR, ".media-activity-marker.ready, .processing-pulse")
            go("/matters/new")
            driver.find_element(By.ID, "matter-name").send_keys("Synthetic second workspace")
            click(".matter-form button[type=submit]")
            wait.until(lambda d: "/setup" in d.current_url)
            click("[data-activity-toggle]")
            wait.until(lambda d: "Synthetic recording checks" in d.find_element(By.CSS_SELECTOR, "[data-activity-content]").text)
            wait.until(lambda d: "needs review" in d.find_element(By.CSS_SELECTOR, "[data-activity-content]").text.lower())
            assert int(driver.find_element(By.CSS_SELECTOR, "[data-activity-badge]").text) >= 1
            go(prefix + f"/sources/{quiet_token}")
            checks.append("Upload polling finishes after a held check; recording review stays actionable in its panel and global Activity after switching matters")

            driver.set_window_size(390, 844)
            toggle = driver.find_elements(By.CSS_SELECTOR, "[data-rail-toggle]")
            if toggle and toggle[0].get_attribute("aria-expanded") == "true":
                toggle[0].click()
            wait.until(lambda d: d.execute_script(
                "return document.querySelector('[data-matter-rail]').getBoundingClientRect().right <= 1"))
            driver.execute_script("document.querySelector('[data-recording-check]').scrollIntoView({block:'center',behavior:'instant'})")
            assert driver.execute_script("return document.documentElement.scrollWidth <= innerWidth + 1")
            driver.save_screenshot(str(args.output / "mobile-recording-decision.png"))
            click("[data-recording-check] button[value=continue]")
            wait.until(lambda _: document.state == "ready")
            assert processor.submissions == 1
            assert processor.input_digest == original_digests[quiet]
            checks.append("Mobile review has no page overflow; explicit transcription submits exactly the original bytes")

            driver.set_window_size(1440, 1000)
            document, late_token = browser_upload(late)
            assert document.state == "ready"
            check = driver.find_element(By.CSS_SELECTOR, "[data-recording-check]")
            assert "Likely speech found" in check.text and "00:30" in check.text
            click("[data-recording-check] [data-seek-ms]")
            wait.until(lambda d: d.execute_script("return document.querySelector('[data-media-player]').currentTime >= 30"))
            driver.execute_script("document.querySelector('[data-media-player]').pause()")
            assert processor.submissions == 2
            assert processor.input_digest == original_digests[late]
            driver.refresh()
            assert "00:30" in driver.find_element(By.CSS_SELECTOR, "[data-recording-check]").text
            driver.save_screenshot(str(args.output / "original-time-speech.png"))
            checks.append("Later speech retains original 30-second offset; jump-to-speech seeks the player and saved findings survive reload")
            assert all(hashlib.sha256(p.read_bytes()).hexdigest() == digest for p, digest in original_digests.items())
            checks.append("All selected external synthetic originals remain byte-identical")
            (args.output / "result.json").write_text(json.dumps({"result": "PASS", "checks": checks}, indent=2) + "\n")
            print(json.dumps({"result": "PASS", "checks": checks}))
        except Exception:
            driver.save_screenshot(str(args.output / "failure.png"))
            (args.output / "failure.html").write_text(driver.page_source)
            raise
        finally:
            driver.quit()
            server.should_exit = True
            thread.join(timeout=15)
            listener.close()


if __name__ == "__main__":
    main()
