#!/usr/bin/env python3
"""Exercise the synthetic Pump Cedar investigation and mixed-source Report in Chrome.

Uses generated speech, controlled transcription/ranking, and a source-echo client.
This verifies workflow and provenance boundaries, not ASR or model quality.
"""

from __future__ import annotations

from case_intelligence.legacy_names import (
    LEGACY_STEM,
)

from browser_navigation import navigate_to

import argparse
import hashlib
import io
import json
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse
import wave
import zipfile

import httpx
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
import uvicorn
from selenium import webdriver
from selenium.common.exceptions import WebDriverException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "src")]
from case_intelligence.workbench import create_workbench_app
from tests.test_matter_media_workflow import ImmediateMediaProcessor

ACTOR = "development-taylor-morgan"
INSPECTION = ("During a training exercise, Pump Cedar showed an 18 psi reading while a reference "
              "gauge showed 12 psi. The operator paused the exercise. The reason for the difference "
              "was not yet established.")
MAINTENANCE = ("A technician replaced Pump Cedar's sensor after the exercise. The entry does not "
               "record a follow-up comparison against the reference gauge.")
TRANSCRIPT = ("I saw the operator stop the exercise after comparing two readings.",
              "I did not watch the sensor replacement", "or any later measurement.")
QUESTION = "What can we establish about the discrepancy, what happened afterward, and what remains unverified?"
INVESTIGATE = "Compare supported agreements, conflicts, and unresolved questions across the documents and recording about Pump Cedar."


def pdf(pages):
    writer = PdfWriter()
    for text in pages:
        page = writer.add_blank_page(width=612, height=792)
        font = DictionaryObject({NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"), NameObject("/BaseFont"): NameObject("/Helvetica")})
        page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"):
            DictionaryObject({NameObject("/F1"): writer._add_object(font)})})
        stream = DecodedStreamObject()
        lines = [line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
                 for line in textwrap.wrap(text, 75)]
        stream.set_data(("BT /F1 12 Tf 36 750 Td 16 TL " + " ".join(
            "(" + line + ") Tj T*" for line in lines) + " ET").encode())
        page[NameObject("/Contents")] = writer._add_object(stream)
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def speech(root):
    """Generate each spoken segment separately so timestamps match the audio."""
    frames, segments, cursor = [], [], 0
    for index, text in enumerate(TRANSCRIPT):
        destination = root / f"segment-{index}.wav"
        if shutil.which("say"):
            intermediate = root / f"segment-{index}.aiff"
            subprocess.run(["say", "-r", "165", "-o", str(intermediate), text], check=True)
            subprocess.run(["ffmpeg", "-v", "error", "-i", str(intermediate), "-ar", "16000",
                            "-ac", "1", str(destination)], check=True)
        elif shutil.which("espeak"):
            intermediate = root / f"segment-{index}-speech.wav"
            subprocess.run(["espeak", "-s", "165", "-w", str(intermediate), text], check=True)
            subprocess.run(["ffmpeg", "-v", "error", "-i", str(intermediate), "-ar", "16000",
                            "-ac", "1", str(destination)], check=True)
        else:
            raise RuntimeError("Synthetic speech generation requires local say or espeak, plus ffmpeg.")
        with wave.open(str(destination), "rb") as recording:
            data = recording.readframes(recording.getnframes())
        duration = len(data) / 32000
        segments.append({"text": text, "model_text": text, "start": cursor,
                         "end": cursor + duration, "overlap": False})
        frames.append(data)
        cursor += duration
    target = root / "Staff recollection recording.wav"
    with wave.open(str(target), "wb") as recording:
        recording.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        recording.writeframes(b"".join(frames))
    return target, segments


class SourceEcho:
    available = True

    def generate(self, *, evidence, **kwargs):
        return {"answerable": bool(evidence), "claims": [
            {"text": ("The machine transcript appears to say that " if item.evidence_kind == "transcript" else "")
             + item.excerpt.split(". ", 1)[0], "evidence_ids": [item.evidence_id]} for item in evidence[:8]],
            "limitation": None, "missing_information": ""}


def main():
    from synthetic_browser_environment import isolate_environment
    isolate_environment()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chrome-binary", type=Path, required=True)
    parser.add_argument("--chromedriver", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--expect-baseline-rejection", action="store_true")
    parser.add_argument("--serve-only", action="store_true", help="Keep the generated app open for manual browser inspection.")
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    receipt = {"synthetic_only": True, "passed": False,
        "scope": "Generated speech; controlled transcription/ranking; source echo; real persistence, verifier, UI and exports.",
        "baseline_rejection_expected": args.expect_baseline_rejection, "checks": []}
    receipt["revision"] = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    receipt["tested_file_sha256"] = {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in (
        "src/case_intelligence/workbench.py", "src/case_intelligence/report_materials.py",
        "src/case_intelligence/work_product_exports.py", "scripts/browser-accept-workflow-handoff.py")}
    def checked(text):
        receipt["checks"].append(text)
        print(text, flush=True)
    original_run, original_popen = subprocess.run, subprocess.Popen
    mapped = {f"/usr/bin/{name}": shutil.which(name) for name in ("ffprobe", "ffmpeg")
              if not Path(f"/usr/bin/{name}").exists() and shutil.which(name)}
    def command(args):
        return [mapped.get(args[0], args[0]), *args[1:]] if isinstance(args, (list, tuple)) and args else args
    def run(args, *pos, **kwargs):
        return original_run(command(args), *pos, **kwargs)
    def popen(args, *pos, **kwargs):
        if sys.platform == "darwin" and isinstance(args, list) and len(args) > 1 and str(args[1]).endswith("/pdf_extract_helper.py"):
            # Native fixture accommodation only. Linux runs the unchanged
            # address-space limit; CPU, file, descriptor and parent time limits
            # remain active here. This is explicitly recorded in the receipt.
            bridge = ("import resource,runpy,sys; old=resource.setrlimit; "
                      "resource.setrlimit=lambda k,v: None if k==resource.RLIMIT_AS else old(k,v); "
                      "sys.argv=sys.argv[1:]; runpy.run_path(sys.argv[0],run_name='__main__')")
            args = [args[0], "-c", bridge, *args[1:]]
        return original_popen(command(args), *pos, **kwargs)
    receipt["host_tool_mapping"] = sorted(Path(name).name for name in mapped)
    receipt["native_pdf_address_space_limit_omitted"] = sys.platform == "darwin"
    with tempfile.TemporaryDirectory(prefix=f"{LEGACY_STEM}-cedar-browser-") as temporary, \
            patch("subprocess.run", run), patch("subprocess.Popen", popen):
        root = Path(temporary).resolve()
        audio, segments = speech(root)
        class Processor(ImmediateMediaProcessor):
            def transcript(self, owner, external_job_id):
                value = super().transcript(owner, external_job_id)
                value["segments"] = [{**old, **new, "speaker": {"cluster_id": "SPEAKER_00",
                    "display_name": "SPEAKER_00", "identity_state": "cluster"}}
                                     for old, new in zip(value["segments"], segments)]
                return value
        app = create_workbench_app(root / "runtime", generator=SourceEcho(), auth_mode="test",
                                   media_processor=Processor(), media_poll_seconds=.01)
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        base = f"http://127.0.0.1:{listener.getsockname()[1]}"
        server = uvicorn.Server(uvicorn.Config(app, log_level="warning", access_log=False))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()
        driver = None
        client = None
        try:
            deadline = time.monotonic() + 30
            while not server.started and time.monotonic() < deadline:
                time.sleep(.02)
            assert server.started
            client = httpx.Client(base_url=base, timeout=30, follow_redirects=False)
            response = client.post("/matters", data={"name": "Synthetic Pump Cedar workflow"})
            slug = response.headers["location"].split("/")[2]
            bench = app.state.workbench
            matter = bench.matter(slug, ACTOR)
            files = {"Inspection note.pdf": (pdf([INSPECTION]), "application/pdf"),
                     "Maintenance entry.txt": (MAINTENANCE.encode(), "text/plain"),
                     "Calibration comparison.pdf": (pdf([
                         "The exercise display exceeded the reference gauge by 6 psi.",
                         "The maintenance entry lacks a documented verification measurement."]), "application/pdf"),
                     "Break-room inventory.txt": (b"The inventory lists four cups and two chairs.", "text/plain"),
                     audio.name: (audio.read_bytes(), "audio/wav")}
            for name, (data, mime) in files.items():
                (root / name).write_bytes(data)
                response = client.post(f"/matters/{slug}/uploads", files=[("files", (name, data, mime))])
                assert response.status_code == 303
            store = bench.source_store(matter)
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                documents = {item.display_name: item for item in store.documents.values()}
                if len(documents) == 5 and all(item.state == "ready" for item in documents.values()):
                    break
                time.sleep(.02)
            assert len(documents) == 5 and all(item.state == "ready" for item in documents.values()), documents
            receipt["source_states"] = {name: document.state for name, document in documents.items()}
            assert len(documents["Calibration comparison.pdf"].parsed_units()) == 2
            citations = {name: tuple(bench._citation(matter, bench._candidate(matter, document, unit, index))
                for index, unit in enumerate(document.parsed_units(), 1)) for name, document in documents.items()}
            assert len(citations[audio.name]) == 3
            primary = (citations["Inspection note.pdf"][0], citations["Maintenance entry.txt"][0])
            anchor = citations[audio.name][1]
            def search(matter_arg, query, **kwargs):
                assert matter_arg.matter_id == matter.matter_id
                if kwargs.get("document_ids") is None:
                    return (*primary, primary[0])
                assert kwargs["document_ids"] == frozenset({documents[audio.name].document_id})
                return (anchor, anchor)
            bench.search = search
            checked("Five fresh sources ingested; two PDF pages and three spoken transcript moments extracted.")
            prefix = f"/matters/{slug}"
            (output / "fixture.json").write_text(json.dumps({"url": base + prefix, "speech_segments": segments}, indent=2))
            if args.serve_only:
                print(base + prefix, flush=True)
                while not server.should_exit:
                    time.sleep(.5)
                return
            downloads = root / "downloads"
            downloads.mkdir()
            options = Options()
            options.binary_location = str(args.chrome_binary)
            options.page_load_strategy = "none"
            for flag in ("--headless=new", "--no-proxy-server", "--window-size=1440,1000"):
                options.add_argument(flag)
            options.add_experimental_option("prefs", {"download.default_directory": str(downloads),
                                                       "download.prompt_for_download": False})
            driver = webdriver.Chrome(service=Service(str(args.chromedriver)), options=options)
            driver.set_script_timeout(30)
            wait = WebDriverWait(driver, 45)
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
                wait.until(lambda d: d.execute_script("return document.readyState") == "complete" and
                           urlparse(d.current_url).path == urlparse(base + path).path)
            def click(selector):
                element = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, selector)))
                driver.execute_script("arguments[0].scrollIntoView({block:'center',behavior:'instant'});", element)
                element.click()
            go(prefix)
            driver.find_element(By.CSS_SELECTOR, 'textarea[name="question"]').send_keys(QUESTION + " Compare the documents and recording.")
            click('button[name="review_task"][value="answer"]')
            wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, ".answer-completion-bar")))
            conversation = bench.workspace.get_conversation(matter.matter_id)
            saved = bench.workspace.messages(matter.matter_id, conversation.conversation_id)[-1]
            assert saved.payload["kind"] == "generated"
            kinds = {ref["evidence_kind"] for claim in saved.payload["claims"] for ref in claim["citations"]}
            assert kinds == {"document", "transcript"}
            driver.save_screenshot(str(output / "mixed-answer.png"))
            checked("Focused answer completed in Chrome with document and transcript citations.")
            for kind in ("document", "transcript"):
                ref = next(ref for claim in saved.payload["claims"] for ref in claim["citations"] if ref["evidence_kind"] == kind)
                go(ref["href"])
                wait.until(lambda d: ref["source_name"] in d.find_element(By.TAG_NAME, "body").text)
                source = client.get(ref["href"])
                assert source.status_code == 200
                if kind == "transcript":
                    player = wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "audio,video")))
                    wait.until(lambda d: d.execute_script("return arguments[0].readyState >= 1", player))
                    # The source route supplies the requested moment; check the player metadata and support resolver.
                    support = bench.support(matter, ref["support_token"])
                    assert support.location == ref["location"]
                    wait.until(lambda d: abs(d.execute_script("return arguments[0].currentTime", player)
                                             - support.start_ms / 1000) < .25)
                driver.save_screenshot(str(output / (kind + "-source.png")))
            checked("Original PDF and recording readers opened from saved citations; transcript moment resolved.")
            go(prefix + f"?conversation={conversation.conversation_id}")
            click('.answer-completion-bar a[href*="/reports/new"]')
            wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, ".report-compose-submit")))
            click('.report-compose-submit button')
            deadline = time.monotonic() + 45
            while time.monotonic() < deadline:
                jobs = bench.report_compilation.jobs.list(matter.matter_id, ACTOR)
                if jobs and jobs[0].state in {"succeeded", "failed"}:
                    break
                time.sleep(.02)
            compilation = jobs[0]
            receipt["mixed_report_state"] = compilation.state
            receipt["mixed_report_message"] = compilation.message
            if args.expect_baseline_rejection:
                assert compilation.state == "failed"
                assert "Some selected source support changed or lacks an exact saved text version." in compilation.message
                checked("Baseline mixed conversation Report rejected missing exact saved text.")
            else:
                assert compilation.state == "succeeded", compilation.message
                go(prefix + f"/reports?report={compilation.report_id}")
                wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, ".report-reading-page")))
                assert driver.find_elements(By.CSS_SELECTOR, ".report-reading-sources a")
                driver.save_screenshot(str(output / "mixed-report.png"))
                for fmt in ("markdown", "docx"):
                    response = client.get(prefix + f"/reports/{compilation.report_id}/export?format={fmt}")
                    assert response.status_code == 200
                    if fmt == "docx":
                        with zipfile.ZipFile(io.BytesIO(response.content)) as word:
                            text = word.read("word/document.xml").decode()
                    else:
                        text = response.text
                    assert "Inspection note.pdf" in text and audio.name in text
                    assert "sensor replacement" in text and "18 psi" in text
                checked("Make report compiled unchanged mixed conversation; Word and Markdown retain source names and supported text.")
            go(prefix + f"?conversation={conversation.conversation_id}")
            field = driver.find_element(By.CSS_SELECTOR, 'textarea[name="question"]')
            field.clear()
            field.send_keys(INVESTIGATE)
            click('button[name="review_task"][value="research"]')
            deadline = time.monotonic() + 45
            while time.monotonic() < deadline:
                investigations = bench.workspace.research_jobs(matter.matter_id, ACTOR)
                if investigations and investigations[0].state in {"succeeded", "failed"}:
                    break
                time.sleep(.02)
            investigation = investigations[0]
            receipt["investigation_state"] = investigation.state
            receipt["investigation_message"] = investigation.message
            receipt["first_pass"] = {key: investigation.result["passes"][0][key]
                for key in ("hit_count", "candidate_passages", "candidate_sources", "selected_passages", "new_evidence", "analyzed_units")}
            go(prefix + f"/research?job={investigation.job_id}")
            if args.expect_baseline_rejection:
                assert investigation.state == "failed"
                assert investigation.message == "The synthesis checkpoint could not be verified."
                assert receipt["first_pass"]["selected_passages"] > receipt["first_pass"]["hit_count"]
                checked("Baseline investigation rejected real producer checkpoint with 3 candidates and 5 selected passages.")
            else:
                assert investigation.state == "succeeded", investigation.message
                assert receipt["first_pass"]["hit_count"] == receipt["first_pass"]["selected_passages"] == 5
                for fmt in ("markdown", "docx", "json"):
                    exported = bench.export_research_work_product(matter, investigation, fmt)
                    if fmt == "docx":
                        with zipfile.ZipFile(io.BytesIO(exported.body)) as word:
                            text = word.read("word/document.xml").decode()
                    else:
                        text = exported.body.decode()
                    assert "Inspection note.pdf" in text and audio.name in text and "18 psi" in text
                    assert documents[audio.name].version_id in text
                    if fmt == "json":
                        portable = json.loads(text)["investigation"]
                        assert portable["findings"][0]["hit_count"] == 5
                        assert portable["coverage"]["passages_considered"] == investigation.result["candidate_count"]
                response = client.post(prefix + f"/research/{investigation.job_id}/report")
                assert response.status_code == 303 and "/reports?report=" in response.headers["location"]
                go(response.headers["location"])
                wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, ".report-reading-page")))
                click('.report-reading-sources a')
                wait.until(lambda d: "support=" in d.current_url)
                wait.until(lambda d: "Inspection note.pdf" in d.find_element(By.TAG_NAME, "body").text)
                checked("Investigate more deeply completed with 5 truthful candidate passages; exports and Report conversion succeeded.")
            driver.save_screenshot(str(output / "investigation-result.png"))
            receipt["passed"] = True
        except Exception:
            if driver:
                driver.save_screenshot(str(output / "failure.png"))
                (output / "failure.html").write_text(driver.page_source)
            raise
        finally:
            (output / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
            try:
                if driver:
                    driver.quit()
            finally:
                if client:
                    client.close()
                server.should_exit = True
                thread.join(timeout=10)
                listener.close()
                if thread.is_alive():
                    raise RuntimeError("Synthetic application did not stop within its cleanup deadline.")


if __name__ == "__main__":
    main()
