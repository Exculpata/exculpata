#!/usr/bin/env python3
"""Run the synthetic loose-file preflight through the real browser path."""

from __future__ import annotations

from case_intelligence.legacy_names import (
    LEGACY_STEM,
)

from browser_navigation import navigate_to

import argparse
import base64
import json
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

import uvicorn
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from case_intelligence.generation import UnavailableGenerator  # noqa: E402
from case_intelligence.malware_scan import (  # noqa: E402
    MalwareScanResult,
    MalwareScannerStatus,
)
from case_intelligence.managed_storage import StoragePolicy  # noqa: E402
from case_intelligence.source_locations import SourceLocationRegistry  # noqa: E402
from case_intelligence.workbench import create_workbench_app  # noqa: E402


ACTOR = "development-taylor-morgan"


class SyntheticUnavailableScanner:
    def __init__(self) -> None:
        self.status_calls = 0
        self.scan_calls = 0
        self.scripted_statuses: list[str] = []

    def status(self, *, force: bool = False) -> MalwareScannerStatus:
        del force
        self.status_calls += 1
        state = (
            self.scripted_statuses.pop(0)
            if self.scripted_statuses
            else "unavailable"
        )
        return MalwareScannerStatus(
            state,
            "synthetic",
            message=f"Synthetic scanner is {state}.",
        )

    def scan(self, _path: Path) -> MalwareScanResult:
        self.scan_calls += 1
        return MalwareScanResult("unavailable", "synthetic")


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _wait_until(predicate, message: str, *, seconds: float = 15.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError(message)


def _fixtures(root: Path) -> list[Path]:
    ready = root / "ready"
    attention = root / "attention"
    unsupported = root / "unsupported"
    over_limit = root / "over-limit"
    for directory in (ready, attention, unsupported, over_limit):
        directory.mkdir(parents=True)
    files = [
        ready / "incident-notes.txt",
        ready / "damaged.pdf",
        attention / "scan.png",
        unsupported / "messages.zip",
        over_limit / "oversize.txt",
    ]
    files[0].write_bytes(b"Synthetic intake note.\n")
    files[1].write_bytes(b"not-a-valid-pdf!!!!")
    files[2].write_bytes(
        base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
        )
    )
    files[3].write_bytes(b"PK\x03\x04")
    files[4].write_bytes(b"x" * 129)
    _require([path.stat().st_size for path in files] == [23, 19, 68, 4, 129], "fixture byte sizes changed")
    return files


def _network_urls(driver: webdriver.Chrome) -> list[str]:
    urls: list[str] = []
    for entry in driver.get_log("performance"):
        try:
            message = json.loads(entry["message"])["message"]
        except (KeyError, TypeError, json.JSONDecodeError):
            continue
        if message.get("method") != "Network.requestWillBeSent":
            continue
        url = message.get("params", {}).get("request", {}).get("url")
        if isinstance(url, str):
            urls.append(url)
    return urls


def _name_collection(driver: webdriver.Chrome) -> None:
    field = driver.find_element(By.CSS_SELECTOR, "[data-upload-collection-name]")
    if not field.get_attribute("value").strip():
        previous_focus = driver.switch_to.active_element
        field.send_keys("Synthetic preflight collection")
        driver.execute_script("arguments[0].focus({preventScroll:true});", previous_focus)


def _select(input_element, files: list[Path]) -> None:
    _name_collection(input_element.parent)
    input_element.send_keys("\n".join(str(path.resolve()) for path in files))


def _replace_selection(driver: webdriver.Chrome, input_element, files: list[Path]) -> None:
    driver.execute_script("arguments[0].value = '';", input_element)
    _select(input_element, files)


def _synthetic_selection(
    driver: webdriver.Chrome,
    input_element,
    count: int,
    *,
    long_paths: bool,
    cross_boundary_duplicate: bool = True,
    file_size: int = 1,
    suffix: str = "txt",
    media_type: str = "text/plain",
    oversized_first_duplicate: bool = False,
    capacity_gap: bool = False,
) -> dict[str, int]:
    _name_collection(driver)
    return driver.execute_script(
        """
        const input = arguments[0];
        const count = arguments[1];
        const longPaths = arguments[2];
        const crossBoundaryDuplicate = arguments[3];
        const fileSize = arguments[4];
        const suffix = arguments[5];
        const mediaType = arguments[6];
        const oversizedFirstDuplicate = arguments[7];
        const capacityGap = arguments[8];
        const transfer = new DataTransfer();
        const segments = ["a", "b", "c", "d"].map((value) => value.repeat(180));
        const descriptors = [];
        for (let index = 0; index < count; index += 1) {
          let name = `record-${String(index).padStart(5, "0")}.${suffix}`;
          if (crossBoundaryDuplicate && count > 2000 && index === 1999) name = `Straße.${suffix}`;
          if (crossBoundaryDuplicate && count > 2000 && index === 2000) name = `STRASSE.${suffix}`;
          if (capacityGap && index === 4096) name = `CapacityGap.${suffix}`;
          if (capacityGap && index === 4097) name = `CAPACITYGAP.${suffix}`;
          const folder = longPaths ? `Production/${segments.join("/")}` : "Production";
          const relativePath = `${folder}/${name}`;
          const selectedSize = oversizedFirstDuplicate && index === 1999
            ? 129
            : capacityGap && index === 4096
              ? 2
            : fileSize;
          const file = new File(["x".repeat(selectedSize)], name, {
            type: mediaType,
            lastModified: 1700000000000 + index,
          });
          Object.defineProperty(file, "webkitRelativePath", { value: relativePath });
          transfer.items.add(file);
          descriptors.push({
            name: file.name,
            relative_path: relativePath,
            size: file.size,
            media_type: file.type,
          });
        }
        const serializedBytes = new TextEncoder().encode(JSON.stringify({
          selection_nonce: "0".repeat(32),
          files: descriptors,
        })).byteLength;
        input.value = "";
        input.files = transfer.files;
        input.dispatchEvent(new Event("change", { bubbles: true }));
        return { count: transfer.files.length, serialized_bytes: serializedBytes };
        """,
        input_element,
        count,
        long_paths,
        cross_boundary_duplicate,
        file_size,
        suffix,
        media_type,
        oversized_first_duplicate,
        capacity_gap,
    )


def _upload_plan_fingerprint(driver: webdriver.Chrome, input_element) -> str:
    fingerprint = driver.execute_async_script(
        """
        const input = arguments[0];
        const done = arguments[arguments.length - 1];
        const form = document.querySelector('[data-upload-form]');
        const maximumItems = Number(form.dataset.maxUploadBatchItems);
        const maximumBytes = Number(form.dataset.maxCollectionBytes);
        const batches = [];
        let batch = [];
        let bytes = 0;
        Array.from(input.files).forEach((file) => {
          if (batch.length && (batch.length >= maximumItems || bytes + file.size > maximumBytes)) {
            batches.push(batch);
            batch = [];
            bytes = 0;
          }
          batch.push(file);
          bytes += file.size;
        });
        if (batch.length) batches.push(batch);
        const plan = batches.map((files) => files.map((file) => ({
          name: String(file.name || '').normalize('NFC'),
          relative_path: String(file.webkitRelativePath || file.name || '').normalize('NFC'),
          size: Number(file.size),
          media_type: String(file.type || '').trim().toLowerCase(),
          last_modified: Number(file.lastModified),
        })));
        crypto.subtle.digest(
          'SHA-256',
          new TextEncoder().encode(JSON.stringify({ version: 1, batches: plan })),
        ).then((digest) => done(
          Array.from(new Uint8Array(digest), (value) => value.toString(16).padStart(2, '0')).join('')
        )).catch((error) => done({ error: String(error) }));
        """,
        input_element,
    )
    _require(
        isinstance(fingerprint, str) and len(fingerprint) == 64,
        "browser could not reproduce the deterministic upload-plan fingerprint",
    )
    return fingerprint


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--chrome-binary", type=Path, required=True)
    parser.add_argument("--chromedriver", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    for label, executable in (
        ("Chrome", args.chrome_binary),
        ("ChromeDriver", args.chromedriver),
    ):
        if not executable.is_file():
            raise SystemExit(f"{label} executable does not exist: {executable}")
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)

    scanner = SyntheticUnavailableScanner()
    report: dict[str, object] = {
        "schema_version": 1,
        "acceptance": "slice1a-loose-file-preflight",
        "synthetic_only": True,
        "passed": False,
        "checks": [],
    }
    driver: webdriver.Chrome | None = None
    server: uvicorn.Server | None = None
    server_thread: threading.Thread | None = None
    server_socket: socket.socket | None = None

    with tempfile.TemporaryDirectory(prefix=f"{LEGACY_STEM}-slice1a-browser-") as temporary:
        temporary_root = Path(temporary)
        files = _fixtures(temporary_root / "fixtures")
        app = create_workbench_app(
            temporary_root / "runtime",
            generator=UnavailableGenerator(),
            auth_mode="test",
            background_ingestion=True,
            source_registry=SourceLocationRegistry(),
            storage_policy=StoragePolicy(
                matter_quota_bytes=4_096,
                upload_session_bytes=512,
                media_file_bytes=256,
                document_file_bytes=128,
                reserve_bytes=0,
            ),
            malware_scanner=scanner,
            malware_scan_mode="extended",
        )

        server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server_socket.bind(("127.0.0.1", 0))
        port = int(server_socket.getsockname()[1])
        config = uvicorn.Config(
            app,
            host="127.0.0.1",
            port=port,
            log_level="warning",
            access_log=False,
        )
        server = uvicorn.Server(config)
        server_thread = threading.Thread(
            target=server.run,
            kwargs={"sockets": [server_socket]},
            name="slice1a-browser-server",
            daemon=True,
        )
        server_thread.start()
        _wait_until(lambda: server.started, "synthetic loopback server did not start")

        options = Options()
        options.page_load_strategy = "none"
        options.binary_location = str(args.chrome_binary)
        options.add_argument("--headless=new")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--disable-gpu")
        options.add_argument("--window-size=1536,1024")
        options.set_capability(
            "goog:loggingPrefs", {"browser": "ALL", "performance": "ALL"}
        )
        try:
            driver = webdriver.Chrome(
                service=Service(executable_path=str(args.chromedriver)),
                options=options,
            )
            driver.set_page_load_timeout(20)
            driver.set_script_timeout(60)
            wait = WebDriverWait(driver, 20)
            base_url = f"http://127.0.0.1:{port}"
            navigate_to(driver, f"{base_url}/matters/new", diagnostics=args.output, page_load_timeout=20, readiness_timeout=20)
            driver.find_element(By.ID, "matter-name").send_keys(
                "Synthetic Slice 1A Browser Matter"
            )
            driver.find_element(By.ID, "matter-descriptor").send_keys(
                "Generated selection preflight acceptance"
            )
            driver.find_element(By.CSS_SELECTOR, ".matter-form button[type='submit']").click()
            wait.until(lambda current: "/setup" in current.current_url)
            slug = urlparse(driver.current_url).path.split("/")[2]
            driver.find_element(By.CSS_SELECTOR, "[data-assistant-collapse]").click()
            wait.until(
                lambda current: "assistant-collapsed"
                in current.find_element(By.TAG_NAME, "body").get_attribute("class").split()
            )
            bench = app.state.workbench
            matter = bench.matter(slug, ACTOR)
            _require(bench.workspace.source_collections(matter.matter_id) == (), "new matter unexpectedly has a collection")
            _require(bench.workspace.recent_upload_sessions(matter.matter_id, ACTOR) == (), "new matter unexpectedly has an upload session")

            driver.execute_cdp_cmd(
                "Emulation.setScriptExecutionDisabled", {"value": True}
            )
            try:
                driver.refresh()
                no_script_folder_input = driver.find_element(By.ID, "source-folder")
                no_script_folder_chooser = driver.find_element(
                    By.CSS_SELECTOR, "[data-folder-chooser]"
                )
                _require(
                    not no_script_folder_input.is_enabled()
                    and not no_script_folder_input.is_displayed()
                    and no_script_folder_input.get_attribute("hidden") is not None
                    and no_script_folder_input.get_attribute("disabled") is not None
                    and no_script_folder_input.get_attribute("tabindex") == "-1"
                    and not no_script_folder_chooser.is_displayed(),
                    "folder selection remained interactive without JavaScript",
                )
                driver.save_screenshot(str(output / "no-javascript-folder.png"))
            finally:
                driver.execute_cdp_cmd(
                    "Emulation.setScriptExecutionDisabled", {"value": False}
                )
            driver.refresh()
            wait.until(
                lambda current: current.find_element(
                    By.CSS_SELECTOR, "[data-folder-chooser]"
                ).is_displayed()
            )
            folder_input = driver.find_element(By.ID, "source-folder")
            folder_chooser = driver.find_element(
                By.CSS_SELECTOR, "[data-folder-chooser]"
            )
            _require(
                folder_input.is_enabled()
                and folder_input.get_attribute("hidden") is None
                and folder_input.get_attribute("disabled") is None
                and folder_input.get_attribute("tabindex") is None
                and folder_chooser.tag_name == "button"
                and folder_chooser.is_enabled(),
                "JavaScript did not expose an accessible folder chooser",
            )
            driver.execute_script("window.syntheticFolderClick=false; arguments[0].click=() => {window.syntheticFolderClick=true;};", folder_input)
            folder_chooser.send_keys(Keys.ENTER)
            _require(driver.execute_script("return window.syntheticFolderClick"), "Keyboard folder action did not open its picker")
            driver.execute_script("delete arguments[0].click;", folder_input)

            file_input = driver.find_element(By.CSS_SELECTOR, "[data-file-input]")
            panel = driver.find_element(By.CSS_SELECTOR, "[data-upload-preflight]")
            review_finding_failures: list[str] = []
            malformed_scan_modes = (
                "omitted-required",
                "invalid-capability",
                "invalid-result",
                "unavailable-valid",
            )
            scanner.scripted_statuses = ["ready"] * len(malformed_scan_modes)
            driver.execute_script(
                """
                window.__slice1aMalformedScanOriginalFetch = window.fetch;
                window.__slice1aMalformedScanMode = '';
                window.fetch = async (...args) => {
                  const response = await window.__slice1aMalformedScanOriginalFetch(...args);
                  if (!String(args[0]).includes('/upload-preflight')) return response;
                  const payload = await response.json();
                  const scan = payload.items[0].scan;
                  if (window.__slice1aMalformedScanMode === 'omitted-required') {
                    delete scan.required;
                  } else if (window.__slice1aMalformedScanMode === 'invalid-capability') {
                    scan.capability = 'client-ready';
                  } else if (window.__slice1aMalformedScanMode === 'invalid-result') {
                    scan.result = 'clean';
                  } else if (window.__slice1aMalformedScanMode === 'unavailable-valid') {
                    scan.capability = 'unavailable';
                  }
                  return new Response(JSON.stringify(payload), {
                    status: response.status,
                    statusText: response.statusText,
                    headers: response.headers,
                  });
                };
                """
            )
            for malformed_scan_mode in malformed_scan_modes:
                driver.execute_script(
                    "window.__slice1aMalformedScanMode = arguments[0];",
                    malformed_scan_mode,
                )
                _synthetic_selection(
                    driver,
                    file_input,
                    1,
                    long_paths=False,
                    cross_boundary_duplicate=False,
                    suffix="png",
                    media_type="image/png",
                )
                wait.until(lambda current: panel.get_attribute("aria-busy") is None)
                if not (
                    driver.find_element(
                        By.CSS_SELECTOR, "[data-upload-preflight-state]"
                    ).text
                    == "Selection review paused"
                    and not driver.find_elements(
                        By.CSS_SELECTOR, "[data-upload-preflight-items] > li"
                    )
                    and not driver.find_element(
                        By.CSS_SELECTOR, "[data-upload-preflight-confirm]"
                    ).is_enabled()
                ):
                    review_finding_failures.append(
                        "malformed scanner projection remained eligible instead of "
                        f"failing closed ({malformed_scan_mode})"
                    )
            driver.execute_script(
                """
                window.fetch = window.__slice1aMalformedScanOriginalFetch;
                delete window.__slice1aMalformedScanOriginalFetch;
                delete window.__slice1aMalformedScanMode;
                """
            )

            driver.execute_script(
                """
                window.__slice1aMalformedCapacityOriginalFetch = window.fetch;
                window.__slice1aMalformedCapacityMode = '';
                window.fetch = async (...args) => {
                  const response = await window.__slice1aMalformedCapacityOriginalFetch(...args);
                  if (!String(args[0]).includes('/upload-preflight')) return response;
                  const payload = await response.json();
                  if (window.__slice1aMalformedCapacityMode === 'missing') {
                    delete payload.matter_capacity;
                  } else {
                    payload.matter_capacity = {
                      version: 2,
                      quota_bytes: 4096,
                      used_bytes: 0,
                      total_reserved_bytes: 0,
                      fresh_available_bytes: 4096,
                      checkpoint_validated: false,
                      checkpoint_remaining_bytes: 0,
                      other_reserved_bytes: 0,
                      available_bytes: 4095,
                    };
                  }
                  return new Response(JSON.stringify(payload), {
                    status: response.status,
                    statusText: response.statusText,
                    headers: response.headers,
                  });
                };
                """
            )
            for capacity_mode in ("missing", "inconsistent"):
                driver.execute_script(
                    "window.__slice1aMalformedCapacityMode = arguments[0];",
                    capacity_mode,
                )
                _replace_selection(driver, file_input, [files[0]])
                wait.until(lambda current: panel.get_attribute("aria-busy") is None)
                if not (
                    driver.find_element(
                        By.CSS_SELECTOR, "[data-upload-preflight-state]"
                    ).text
                    == "Selection review paused"
                    and not driver.find_elements(
                        By.CSS_SELECTOR, "[data-upload-preflight-items] > li"
                    )
                    and not driver.find_element(
                        By.CSS_SELECTOR, "[data-upload-preflight-confirm]"
                    ).is_enabled()
                ):
                    review_finding_failures.append(
                        "invalid matter-capacity projection did not fail the preview "
                        f"closed ({capacity_mode})"
                    )
            driver.execute_script(
                """
                window.fetch = window.__slice1aMalformedCapacityOriginalFetch;
                delete window.__slice1aMalformedCapacityOriginalFetch;
                delete window.__slice1aMalformedCapacityMode;
                """
            )

            driver.execute_script(
                """
                window.__slice1aMissingPathAttestationOriginalFetch = window.fetch;
                window.__slice1aPathAttestationMode = '';
                window.fetch = async (...args) => {
                  const response = await window.__slice1aMissingPathAttestationOriginalFetch(...args);
                  if (!String(args[0]).includes('/upload-preflight')) return response;
                  const payload = await response.json();
                  if (window.__slice1aPathAttestationMode === 'missing') {
                    delete payload.items[0].path_safety_validated;
                  } else {
                    payload.items[0].path_safety_validated = false;
                  }
                  return new Response(JSON.stringify(payload), {
                    status: response.status,
                    statusText: response.statusText,
                    headers: response.headers,
                  });
                };
                """
            )
            for path_attestation_mode in ("missing", "false-valid"):
                driver.execute_script(
                    "window.__slice1aPathAttestationMode = arguments[0];",
                    path_attestation_mode,
                )
                _replace_selection(driver, file_input, [files[0]])
                wait.until(lambda current: panel.get_attribute("aria-busy") is None)
                if not (
                    driver.find_element(
                        By.CSS_SELECTOR, "[data-upload-preflight-state]"
                    ).text
                    == "Selection review paused"
                    and not driver.find_elements(
                        By.CSS_SELECTOR, "[data-upload-preflight-items] > li"
                    )
                    and not driver.find_element(
                        By.CSS_SELECTOR, "[data-upload-preflight-confirm]"
                    ).is_enabled()
                ):
                    review_finding_failures.append(
                        "invalid path-safety attestation did not fail the preview "
                        f"closed ({path_attestation_mode})"
                    )
            driver.execute_script(
                """
                window.fetch = window.__slice1aMissingPathAttestationOriginalFetch;
                delete window.__slice1aMissingPathAttestationOriginalFetch;
                delete window.__slice1aPathAttestationMode;
                """
            )

            driver.execute_script(
                """
                window.__slice1aInjectedPathOriginalFetch = window.fetch;
                window.fetch = async (...args) => {
                  const response = await window.__slice1aInjectedPathOriginalFetch(...args);
                  if (!String(args[0]).includes('/upload-preflight')) return response;
                  const payload = await response.json();
                  payload.items[0].display_path = '/private/absolute/evidence.png';
                  return new Response(JSON.stringify(payload), {
                    status: response.status,
                    statusText: response.statusText,
                    headers: response.headers,
                  });
                };
                """
            )
            _replace_selection(driver, file_input, [files[0]])
            wait.until(
                lambda current: len(
                    current.find_elements(
                        By.CSS_SELECTOR, "[data-upload-preflight-items] > li"
                    )
                )
                == 1
                and panel.get_attribute("aria-busy") is None
            )
            injected_path_label = driver.find_element(
                By.CSS_SELECTOR, "[data-upload-preflight-items] strong"
            ).text
            if injected_path_label != "incident-notes.txt":
                review_finding_failures.append(
                    "response-originated absolute display path reached rendered output"
                )
            driver.execute_script(
                """
                window.fetch = window.__slice1aInjectedPathOriginalFetch;
                delete window.__slice1aInjectedPathOriginalFetch;
                """
            )

            scanner.scripted_statuses = ["ready", "unavailable"]
            scanner_snapshot_calls = scanner.status_calls
            _synthetic_selection(
                driver,
                file_input,
                2_001,
                long_paths=False,
                cross_boundary_duplicate=False,
                suffix="png",
                media_type="image/png",
            )
            WebDriverWait(driver, 60).until(
                lambda current: len(
                    current.find_elements(
                        By.CSS_SELECTOR, "[data-upload-preflight-items] > li"
                    )
                )
                == 2_001
                and panel.get_attribute("aria-busy") is None
            )
            scanner_snapshot_rows = driver.find_elements(
                By.CSS_SELECTOR, "[data-upload-preflight-items] > li"
            )
            scanner_snapshot_states = {
                row.get_attribute("data-state") for row in scanner_snapshot_rows
            }
            if not (
                scanner_snapshot_states == {"valid"}
                and driver.find_element(
                    By.CSS_SELECTOR, "[data-upload-preflight-confirm]"
                ).text
                == "Upload 2,001 ready files"
                and "Security scan required and not run yet"
                in scanner_snapshot_rows[0].text
                and "Security scan required and not run yet"
                in scanner_snapshot_rows[-1].text
                and scanner.status_calls == scanner_snapshot_calls + 2
                and not scanner.scripted_statuses
            ):
                review_finding_failures.append(
                    "one logical selection displayed mixed scanner availability across batches"
                )

            driver.execute_script(
                """
                window.__slice1aInvalidSizeOriginalFetch = window.fetch;
                window.fetch = (...args) => {
                  if (!String(args[0]).includes('/upload-preflight')) {
                    return window.__slice1aInvalidSizeOriginalFetch(...args);
                  }
                  const payload = JSON.parse(String(args[1].body));
                  payload.files.forEach((item) => {
                    if (item.name === 'missing-size.txt') delete item.size;
                  });
                  return window.__slice1aInvalidSizeOriginalFetch(args[0], {
                    ...args[1],
                    body: JSON.stringify(payload),
                  });
                };
                """
            )
            driver.execute_script(
                """
                const input = arguments[0];
                const transfer = new DataTransfer();
                ["Folder Alpha", "Folder Beta"].forEach((folder, index) => {
                  const file = new File([`synthetic-${index}`], "same-name.txt", {
                    type: "text/plain",
                    lastModified: 1700000100000 + index,
                  });
                  Object.defineProperty(file, "webkitRelativePath", {
                    value: `${folder}/same-name.txt`,
                  });
                  transfer.items.add(file);
                });
                input.value = "";
                input.files = transfer.files;
                input.dispatchEvent(new Event("change", { bubbles: true }));
                """,
                file_input,
            )
            WebDriverWait(driver, 30).until(
                lambda current: len(
                    current.find_elements(
                        By.CSS_SELECTOR, "[data-upload-preflight-items] > li"
                    )
                )
                == 2
                and panel.get_attribute("aria-busy") is None
            )
            folder_labels = [
                row.find_element(By.TAG_NAME, "strong").text
                for row in driver.find_elements(
                    By.CSS_SELECTOR, "[data-upload-preflight-items] > li"
                )
            ]
            if not (
                folder_labels
                == [
                    "Folder Alpha/same-name.txt",
                    "Folder Beta/same-name.txt",
                ]
                and all(
                    not label.startswith(("/", "\\"))
                    and ":" not in label
                    and str(temporary_root) not in label
                    for label in folder_labels
                )
            ):
                review_finding_failures.append(
                    "folder selection did not safely disambiguate duplicate leaf names"
                )

            driver.execute_script(
                """
                const input = arguments[0];
                const transfer = new DataTransfer();
                const fixtures = [
                  ["Folder Alpha/archive.zip", "archive.zip", 4, "application/zip"],
                  ["Folder Beta/archive.zip", "archive.zip", 4, "application/zip"],
                  ["Folder Alpha/empty.txt", "empty.txt", 0, "text/plain"],
                  ["Folder Beta/empty.txt", "empty.txt", 0, "text/plain"],
                  ["Folder Alpha/missing-size.txt", "missing-size.txt", 1, "text/plain"],
                  ["Folder Beta/missing-size.txt", "missing-size.txt", 1, "text/plain"],
                  ["Folder Alpha/oversize.txt", "oversize.txt", 129, "text/plain"],
                  ["Folder Beta/oversize.txt", "oversize.txt", 129, "text/plain"],
                  ["../private/absolute/evidence.txt", "evidence.txt", 1, "text/plain"],
                ];
                fixtures.forEach(([relativePath, name, size, mediaType], index) => {
                  const file = new File(["x".repeat(size)], name, {
                    type: mediaType,
                    lastModified: 1700000200000 + index,
                  });
                  Object.defineProperty(file, "webkitRelativePath", {
                    value: relativePath,
                  });
                  transfer.items.add(file);
                });
                input.value = "";
                input.files = transfer.files;
                input.dispatchEvent(new Event("change", { bubbles: true }));
                """,
                file_input,
            )
            WebDriverWait(driver, 30).until(
                lambda current: len(
                    current.find_elements(
                        By.CSS_SELECTOR, "[data-upload-preflight-items] > li"
                    )
                )
                == 9
                and panel.get_attribute("aria-busy") is None
            )
            driver.execute_script(
                """
                window.fetch = window.__slice1aInvalidSizeOriginalFetch;
                delete window.__slice1aInvalidSizeOriginalFetch;
                """
            )
            blocked_rows = driver.find_elements(
                By.CSS_SELECTOR, "[data-upload-preflight-items] > li"
            )
            blocked_labels = [
                row.find_element(By.TAG_NAME, "strong").text for row in blocked_rows
            ]
            blocked_states = [
                row.get_attribute("data-state") for row in blocked_rows
            ]
            if not (
                blocked_labels
                == [
                    "Folder Alpha/archive.zip",
                    "Folder Beta/archive.zip",
                    "Folder Alpha/empty.txt",
                    "Folder Beta/empty.txt",
                    "Folder Alpha/missing-size.txt",
                    "Folder Beta/missing-size.txt",
                    "Folder Alpha/oversize.txt",
                    "Folder Beta/oversize.txt",
                    "Selected file 9",
                ]
                and blocked_states
                == [
                    "unsupported",
                    "unsupported",
                    "failed",
                    "failed",
                    "failed",
                    "failed",
                    "over_limit",
                    "over_limit",
                    "failed",
                ]
                and all(
                    "../" not in label
                    and not label.startswith(("/", "\\"))
                    and ":" not in label
                    and str(temporary_root) not in label
                    for label in blocked_labels
                )
            ):
                review_finding_failures.append(
                    "safe blocked folder rows were ambiguous or an unsafe path rendered"
                )
            _require(
                not review_finding_failures,
                "; ".join(review_finding_failures),
            )

            driver.execute_script(
                """
                window.__slice1aScaleOriginalFetch = window.fetch;
                window.__slice1aScaleRequests = [];
                window.fetch = (...args) => {
                  if (String(args[0]).includes('/upload-preflight')) {
                    const body = String(args[1]?.body || '');
                    const payload = JSON.parse(body);
                    window.__slice1aScaleRequests.push({
                      bytes: new TextEncoder().encode(body).byteLength,
                      count: payload.files.length,
                      nonce: payload.selection_nonce,
                    });
                  }
                  return window.__slice1aScaleOriginalFetch(...args);
                };
                """
            )
            scale_status_calls = scanner.status_calls
            scale_selection = _synthetic_selection(
                driver, file_input, 10_000, long_paths=True, capacity_gap=True
            )
            _require(
                scale_selection["serialized_bytes"] > 6 * 1024 * 1024,
                "ten-thousand-row fixture did not exceed the one-request cap",
            )
            WebDriverWait(driver, 120).until(
                lambda current: len(
                    current.find_elements(
                        By.CSS_SELECTOR, "[data-upload-preflight-items] > li"
                    )
                )
                == 10_000
                and current.find_element(
                    By.CSS_SELECTOR, "[data-upload-preflight-confirm]"
                ).text
                == "Upload 4,096 ready files"
            )
            scale_requests = driver.execute_script(
                "return window.__slice1aScaleRequests;"
            )
            _require(len(scale_requests) > 1, "large selection used only one request")
            _require(
                sum(request["count"] for request in scale_requests) == 10_000,
                "large selection did not account for exactly ten thousand rows",
            )
            _require(
                all(request["count"] <= 2_000 for request in scale_requests),
                "a preflight request exceeded the two-thousand-row cap",
            )
            _require(
                all(request["bytes"] <= 6 * 1024 * 1024 for request in scale_requests),
                "a preflight request exceeded the six-MiB cap",
            )
            _require(
                len({request["nonce"] for request in scale_requests}) == 1,
                "large selection did not retain one selection nonce",
            )
            scale_rows = driver.find_elements(
                By.CSS_SELECTOR, "[data-upload-preflight-items] > li"
            )
            _require(
                scale_rows[1999].get_attribute("data-state") == "valid"
                and scale_rows[2000].get_attribute("data-state")
                == "duplicate_candidate",
                "server-canonical duplicate across the batch boundary was not preserved",
            )
            _require(
                scale_rows[4096].get_attribute("data-state") == "over_limit"
                and "matter has 1 B of upload capacity remaining"
                in scale_rows[4096].text
                and scale_rows[4097].get_attribute("data-state") == "valid",
                "a capacity-blocked path suppressed its smaller canonical duplicate",
            )
            _require(
                sum(
                    2 if index == 4096 else 1
                    for index, row in enumerate(scale_rows)
                    if row.get_attribute("data-state") == "valid"
                )
                <= 4_096,
                "preflight marked more ready bytes than matter capacity",
            )
            _require(
                scale_rows[0].find_element(By.TAG_NAME, "strong").text.endswith(
                    "/record-00000.txt"
                )
                and scale_rows[-1]
                .find_element(By.TAG_NAME, "strong")
                .text.endswith("/record-09999.txt"),
                "consolidated preview did not preserve global selection order",
            )
            _require(
                "1 repeated path"
                in driver.find_element(
                    By.CSS_SELECTOR, "[data-upload-preflight-counts]"
                ).text,
                "consolidated preview did not report the cross-batch duplicate",
            )
            _require(
                scanner.status_calls == scale_status_calls + len(scale_requests),
                "large preview did not perform one scanner-status check per bounded request",
            )
            _require(
                bench.workspace.source_collections(matter.matter_id) == ()
                and bench.workspace.recent_upload_sessions(matter.matter_id, ACTOR)
                == ()
                and bench.workspace.pending_upload_bytes(matter.matter_id) == 0,
                "large preflight created durable upload state before confirmation",
            )
            driver.execute_script(
                """
                window.fetch = window.__slice1aScaleOriginalFetch;
                delete window.__slice1aScaleOriginalFetch;
                delete window.__slice1aScaleRequests;
                """
            )

            _synthetic_selection(
                driver,
                file_input,
                2_001,
                long_paths=False,
                oversized_first_duplicate=True,
            )
            WebDriverWait(driver, 60).until(
                lambda current: len(
                    current.find_elements(
                        By.CSS_SELECTOR, "[data-upload-preflight-items] > li"
                    )
                )
                == 2_001
                and panel.get_attribute("aria-busy") is None
            )
            eligible_duplicate_rows = driver.find_elements(
                By.CSS_SELECTOR, "[data-upload-preflight-items] > li"
            )
            _require(
                eligible_duplicate_rows[1999].get_attribute("data-state")
                == "over_limit"
                and eligible_duplicate_rows[2000].get_attribute("data-state")
                == "valid"
                and driver.find_element(
                    By.CSS_SELECTOR, "[data-upload-preflight-confirm]"
                ).text
                == "Upload 2,000 ready files"
                and "0 repeated paths"
                in driver.find_element(
                    By.CSS_SELECTOR, "[data-upload-preflight-counts]"
                ).text,
                "an oversized earlier batch suppressed its usable canonical successor",
            )

            oversized_status_calls = scanner.status_calls
            driver.execute_script(
                """
                window.__slice1aOversizedOriginalFetch = window.fetch;
                window.__slice1aOversizedFetchCount = 0;
                window.fetch = (...args) => {
                  if (String(args[0]).includes('/upload-preflight')) {
                    window.__slice1aOversizedFetchCount += 1;
                  }
                  return window.__slice1aOversizedOriginalFetch(...args);
                };
                const input = arguments[0];
                const transfer = new DataTransfer();
                const file = new File(['x'], 'synthetic.txt', {
                  type: 'text/plain',
                  lastModified: 1700000000000,
                });
                Object.defineProperty(file, 'webkitRelativePath', {
                  value: `Synthetic/${'x'.repeat(6 * 1024 * 1024)}.txt`,
                });
                transfer.items.add(file);
                input.value = '';
                input.files = transfer.files;
                input.dispatchEvent(new Event('change', { bubbles: true }));
                """,
                file_input,
            )
            wait.until(
                lambda current: len(
                    current.find_elements(
                        By.CSS_SELECTOR, "[data-upload-preflight-items] > li"
                    )
                )
                == 1
                and current.find_element(
                    By.CSS_SELECTOR, "[data-upload-preflight-confirm]"
                ).text
                == "Save selection receipt"
            )
            oversized_row = driver.find_element(
                By.CSS_SELECTOR, "[data-upload-preflight-items] > li"
            )
            _require(
                oversized_row.get_attribute("data-state") == "failed"
                and oversized_row.text.startswith("Selected file 1")
                and driver.execute_script(
                    "return window.__slice1aOversizedFetchCount;"
                )
                == 0
                and scanner.status_calls == oversized_status_calls,
                "one oversized descriptor was not handled locally and content-free",
            )
            driver.execute_script(
                """
                window.fetch = window.__slice1aOversizedOriginalFetch;
                delete window.__slice1aOversizedOriginalFetch;
                delete window.__slice1aOversizedFetchCount;
                """
            )

            driver.execute_script(
                """
                window.__slice1aBatchOriginalFetch = window.fetch;
                window.__slice1aBatchRequestCount = 0;
                window.__slice1aSecondBatchPending = false;
                window.fetch = (...args) => {
                  if (!String(args[0]).includes('/upload-preflight')) {
                    return window.__slice1aBatchOriginalFetch(...args);
                  }
                  window.__slice1aBatchRequestCount += 1;
                  if (window.__slice1aBatchRequestCount !== 2) {
                    return window.__slice1aBatchOriginalFetch(...args);
                  }
                  window.__slice1aSecondBatchPending = true;
                  return new Promise((_resolve, reject) => {
                    const signal = args[1]?.signal;
                    signal?.addEventListener(
                      'abort',
                      () => reject(new DOMException('Aborted', 'AbortError')),
                      { once: true },
                    );
                  });
                };
                """
            )
            _synthetic_selection(driver, file_input, 2_001, long_paths=False)
            wait.until(
                lambda current: current.execute_script(
                    "return window.__slice1aSecondBatchPending === true;"
                )
                and panel.get_attribute("aria-busy") == "true"
            )
            _require(
                not driver.find_elements(
                    By.CSS_SELECTOR, "[data-upload-preflight-items] > li"
                ),
                "partial first-batch rows became visible before consolidated completion",
            )
            _replace_selection(driver, file_input, [files[0]])
            wait.until(
                lambda current: len(
                    current.find_elements(
                        By.CSS_SELECTOR, "[data-upload-preflight-items] > li"
                    )
                )
                == 1
                and current.find_element(
                    By.CSS_SELECTOR, "[data-upload-preflight-confirm]"
                ).text
                == "Upload 1 ready file"
            )
            driver.execute_script(
                """
                window.fetch = window.__slice1aBatchOriginalFetch;
                delete window.__slice1aBatchOriginalFetch;
                delete window.__slice1aBatchRequestCount;
                delete window.__slice1aSecondBatchPending;
                """
            )

            checkpoint_matter = bench.create_matter(
                "Synthetic multi-batch checkpoint matter",
                "Generated resume checkpoint isolation acceptance",
                ACTOR,
            )
            navigate_to(driver, f"{base_url}/matters/{checkpoint_matter.slug}/setup", diagnostics=args.output, page_load_timeout=20, readiness_timeout=20)
            file_input = wait.until(
                lambda current: current.find_element(
                    By.CSS_SELECTOR, "[data-file-input]"
                )
            )
            panel = driver.find_element(By.CSS_SELECTOR, "[data-upload-preflight]")
            _synthetic_selection(
                driver,
                file_input,
                2_001,
                long_paths=False,
                cross_boundary_duplicate=False,
            )
            WebDriverWait(driver, 60).until(
                lambda current: current.find_element(
                    By.CSS_SELECTOR, "[data-upload-preflight-confirm]"
                ).text
                == "Upload 2,001 ready files"
            )
            corrupt_plan_fingerprint = _upload_plan_fingerprint(driver, file_input)
            stale_files = [
                {
                    "display_name": f"record-{ordinal:05d}.txt",
                    "relative_path": f"Production/record-{ordinal:05d}.txt",
                    "media_type": "text/plain",
                    "expected_size": 1,
                }
                for ordinal in range(512, 1_024)
            ]
            stale_session, _ = bench.create_upload_session(
                checkpoint_matter,
                ACTOR,
                "Synthetic cancelled resume checkpoint",
                stale_files,
            )
            bench.workspace.cancel_upload_session(
                checkpoint_matter.matter_id, ACTOR, stale_session.upload_session_id
            )
            stale_session, _ = bench.workspace.upload_session(
                checkpoint_matter.matter_id, ACTOR, stale_session.upload_session_id
            )
            stale_collections = bench.workspace.source_collections(
                checkpoint_matter.matter_id
            )
            stale_sessions = bench.workspace.recent_upload_sessions(
                checkpoint_matter.matter_id, ACTOR
            )
            stale_collection = bench.workspace.source_collection(
                checkpoint_matter.matter_id, stale_session.collection_id
            )
            upload_session_key = (
                f"case-intelligence:upload:{checkpoint_matter.slug}"
            )
            driver.execute_script(
                "window.localStorage.setItem(arguments[0], arguments[1]);",
                upload_session_key,
                json.dumps(
                    {
                        "version": 3,
                        "plan_fingerprint": corrupt_plan_fingerprint,
                        "session_id": "",
                        "collection_id": stale_session.collection_id,
                        "batch_index": 1,
                    }
                ),
            )
            driver.execute_script(
                """
                window.__slice1aCorruptOriginalFetch = window.fetch;
                window.__slice1aCorruptBodies = [];
                window.fetch = (...args) => {
                  const method = String(args[1]?.method || 'GET').toUpperCase();
                  if (String(args[0]).includes('/upload-sessions') && method === 'POST') {
                    window.__slice1aCorruptBodies.push(JSON.parse(String(args[1].body)));
                    return Promise.reject(new Error('Synthetic corrupt-state stop'));
                  }
                  return window.__slice1aCorruptOriginalFetch(...args);
                };
                """
            )
            driver.execute_script(
                "arguments[0].click();",
                driver.find_element(
                    By.CSS_SELECTOR, "[data-upload-preflight-confirm]"
                ),
            )
            corrupt_body = WebDriverWait(driver, 30).until(
                lambda current: (
                    bodies[0]
                    if (
                        bodies
                        := current.execute_script(
                            "return window.__slice1aCorruptBodies;"
                        )
                    )
                    else None
                )
            )
            _require(
                corrupt_body["resume_session_id"] == ""
                and corrupt_body["collection_id"] == ""
                and corrupt_body["files"][0]["relative_path"].endswith(
                    "/record-00000.txt"
                )
                and bench.workspace.source_collections(checkpoint_matter.matter_id)
                == stale_collections
                and bench.workspace.recent_upload_sessions(
                    checkpoint_matter.matter_id, ACTOR
                )
                == stale_sessions
                and len(
                    driver.execute_script(
                        "return window.__slice1aCorruptBodies;"
                    )
                )
                == 1,
                "collection-only matching-fingerprint state skipped batch zero or mutated its stale collection",
            )
            driver.execute_script(
                """
                window.fetch = window.__slice1aCorruptOriginalFetch;
                delete window.__slice1aCorruptOriginalFetch;
                delete window.__slice1aCorruptBodies;
                window.localStorage.removeItem(arguments[0]);
                """,
                upload_session_key,
            )
            wait.until(
                lambda current: current.find_element(
                    By.CSS_SELECTOR, "[data-upload-form]"
                ).get_attribute("aria-busy")
                is None
            )

            _synthetic_selection(
                driver,
                file_input,
                2_001,
                long_paths=False,
                cross_boundary_duplicate=False,
            )
            WebDriverWait(driver, 60).until(
                lambda current: current.find_element(
                    By.CSS_SELECTOR, "[data-upload-preflight-confirm]"
                ).is_enabled()
                and current.find_element(
                    By.CSS_SELECTOR, "[data-upload-preflight-confirm]"
                ).text
                == "Upload 2,001 ready files"
            )
            driver.execute_script(
                "window.localStorage.setItem(arguments[0], arguments[1]);",
                upload_session_key,
                json.dumps(
                    {
                        "version": 3,
                        "plan_fingerprint": corrupt_plan_fingerprint,
                        "session_id": stale_session.upload_session_id,
                        "collection_id": stale_session.collection_id,
                        "batch_index": 1,
                    }
                ),
            )
            driver.execute_script(
                """
                window.__slice1aCancelledOriginalFetch = window.fetch;
                window.__slice1aCancelledBodies = [];
                window.fetch = (...args) => {
                  const method = String(args[1]?.method || 'GET').toUpperCase();
                  if (String(args[0]).includes('/upload-sessions') && method === 'POST') {
                    window.__slice1aCancelledBodies.push(JSON.parse(String(args[1].body)));
                  }
                  if (method === 'PUT') {
                    return new Promise((_resolve, reject) => {
                      args[1]?.signal?.addEventListener(
                        'abort',
                        () => reject(new DOMException('Aborted', 'AbortError')),
                        { once: true },
                      );
                    });
                  }
                  return window.__slice1aCancelledOriginalFetch(...args);
                };
                """
            )
            driver.execute_script(
                "arguments[0].click();",
                driver.find_element(
                    By.CSS_SELECTOR, "[data-upload-preflight-confirm]"
                ),
            )
            cancelled_bodies = WebDriverWait(driver, 30).until(
                lambda current: (
                    bodies
                    if len(
                        bodies
                        := current.execute_script(
                            "return window.__slice1aCancelledBodies;"
                        )
                    )
                    == 2
                    else None
                )
            )
            recovered_checkpoint = json.loads(
                WebDriverWait(driver, 30).until(
                    lambda current: (
                        value
                        if (
                            value
                            := current.execute_script(
                                "return window.localStorage.getItem(arguments[0]);",
                                upload_session_key,
                            )
                        )
                        and json.loads(value)["session_id"]
                        != stale_session.upload_session_id
                        else None
                    )
                )
            )
            _require(
                cancelled_bodies[0]["resume_session_id"]
                == stale_session.upload_session_id
                and cancelled_bodies[0]["collection_id"]
                == stale_session.collection_id
                and cancelled_bodies[0]["files"][0]["relative_path"].endswith(
                    "/record-00512.txt"
                )
                and cancelled_bodies[1]["resume_session_id"] == ""
                and cancelled_bodies[1]["collection_id"] == ""
                and cancelled_bodies[1]["files"][0]["relative_path"].endswith(
                    "/record-00000.txt"
                )
                and recovered_checkpoint["batch_index"] == 0
                and recovered_checkpoint["collection_id"]
                != stale_session.collection_id
                and bench.workspace.source_collection(
                    checkpoint_matter.matter_id, stale_session.collection_id
                )
                == stale_collection
                and len(
                    bench.workspace.recent_upload_sessions(
                        checkpoint_matter.matter_id, ACTOR
                    )
                )
                == len(stale_sessions) + 1,
                "cancelled later-batch checkpoint did not retry once from a new batch-zero collection",
            )

            partial_matter = bench.create_matter(
                "Synthetic partial checkpoint matter",
                "Generated completed-batch advancement acceptance",
                ACTOR,
            )
            partial_key = f"case-intelligence:upload:{partial_matter.slug}"
            navigate_to(driver, f"{base_url}/matters/{partial_matter.slug}/setup", diagnostics=args.output, page_load_timeout=20, readiness_timeout=20)
            partial_input = wait.until(
                lambda current: current.find_element(
                    By.CSS_SELECTOR, "[data-file-input]"
                )
            )
            _synthetic_selection(
                driver,
                partial_input,
                5,
                long_paths=False,
                cross_boundary_duplicate=False,
                file_size=128,
            )
            wait.until(
                lambda current: current.find_element(
                    By.CSS_SELECTOR, "[data-upload-preflight-confirm]"
                ).text
                == "Upload 5 ready files"
            )
            partial_fingerprint = _upload_plan_fingerprint(driver, partial_input)
            partial_session, partial_items = bench.create_upload_session(
                partial_matter,
                ACTOR,
                "Synthetic partial first batch",
                [
                    {
                        "display_name": f"record-{ordinal:05d}.txt",
                        "relative_path": f"Production/record-{ordinal:05d}.txt",
                        "media_type": "text/plain",
                        "expected_size": 128,
                    }
                    for ordinal in range(4)
                ],
            )
            for item in partial_items:
                bench.workspace.fail_upload_item(
                    partial_matter.matter_id,
                    ACTOR,
                    partial_session.upload_session_id,
                    item.upload_item_id,
                    "Synthetic completed-batch failure",
                )
            partial_session, partial_items = bench.workspace.upload_session(
                partial_matter.matter_id,
                ACTOR,
                partial_session.upload_session_id,
            )
            _require(
                partial_session.state == "partial"
                and all(item.state == "failed" for item in partial_items),
                "synthetic partial checkpoint was not prepared",
            )
            driver.execute_script(
                "window.localStorage.setItem(arguments[0], arguments[1]);",
                partial_key,
                json.dumps(
                    {
                        "version": 3,
                        "plan_fingerprint": partial_fingerprint,
                        "session_id": partial_session.upload_session_id,
                        "collection_id": partial_session.collection_id,
                        "batch_index": 0,
                    }
                ),
            )
            driver.execute_script(
                """
                window.__slice1aPartialOriginalFetch = window.fetch;
                window.__slice1aPartialBodies = [];
                window.fetch = (...args) => {
                  const method = String(args[1]?.method || 'GET').toUpperCase();
                  if (String(args[0]).includes('/upload-sessions') && method === 'POST') {
                    window.__slice1aPartialBodies.push(JSON.parse(String(args[1].body)));
                  }
                  if (method === 'PUT') {
                    return new Promise((_resolve, reject) => {
                      args[1]?.signal?.addEventListener(
                        'abort',
                        () => reject(new DOMException('Aborted', 'AbortError')),
                        { once: true },
                      );
                    });
                  }
                  return window.__slice1aPartialOriginalFetch(...args);
                };
                """
            )
            driver.execute_script(
                "arguments[0].click();",
                driver.find_element(
                    By.CSS_SELECTOR, "[data-upload-preflight-confirm]"
                ),
            )
            partial_bodies = WebDriverWait(driver, 30).until(
                lambda current: (
                    bodies
                    if len(
                        bodies
                        := current.execute_script(
                            "return window.__slice1aPartialBodies;"
                        )
                    )
                    == 2
                    else None
                )
            )
            advanced_checkpoint = json.loads(
                WebDriverWait(driver, 30).until(
                    lambda current: (
                        value
                        if (
                            value
                            := current.execute_script(
                                "return window.localStorage.getItem(arguments[0]);",
                                partial_key,
                            )
                        )
                        and json.loads(value)["session_id"]
                        != partial_session.upload_session_id
                        else None
                    )
                )
            )
            _require(
                partial_bodies[0]["resume_session_id"]
                == partial_session.upload_session_id
                and partial_bodies[0]["collection_id"]
                == partial_session.collection_id
                and len(partial_bodies[0]["files"]) == 4
                and partial_bodies[1]["resume_session_id"] == ""
                and partial_bodies[1]["collection_id"]
                == partial_session.collection_id
                and len(partial_bodies[1]["files"]) == 1
                and partial_bodies[1]["files"][0]["relative_path"].endswith(
                    "/record-00004.txt"
                )
                and advanced_checkpoint["batch_index"] == 1
                and advanced_checkpoint["collection_id"]
                == partial_session.collection_id
                and advanced_checkpoint["session_id"]
                != partial_session.upload_session_id
                and bench.workspace.upload_session(
                    partial_matter.matter_id,
                    ACTOR,
                    partial_session.upload_session_id,
                )[0].state
                == "partial"
                and len(
                    bench.workspace.recent_upload_sessions(
                        partial_matter.matter_id, ACTOR
                    )
                )
                == 2,
                "exact partial checkpoint was duplicated or did not advance to the next batch",
            )

            capacity_resume_matter = bench.create_matter(
                "Synthetic capacity-bound resume matter",
                "Generated completed-batch capacity acceptance",
                ACTOR,
            )
            capacity_resume_key = (
                f"case-intelligence:upload:{capacity_resume_matter.slug}"
            )
            navigate_to(driver, f"{base_url}/matters/{capacity_resume_matter.slug}/setup", diagnostics=args.output, page_load_timeout=20, readiness_timeout=20)
            capacity_resume_input = wait.until(
                lambda current: current.find_element(
                    By.CSS_SELECTOR, "[data-file-input]"
                )
            )
            _synthetic_selection(
                driver,
                capacity_resume_input,
                5,
                long_paths=False,
                cross_boundary_duplicate=False,
                file_size=128,
            )
            wait.until(
                lambda current: current.find_element(
                    By.CSS_SELECTOR, "[data-upload-preflight-confirm]"
                ).text
                == "Upload 5 ready files"
            )
            driver.execute_script(
                """
                window.__slice1aCapacityBootstrapOriginalFetch = window.fetch;
                window.fetch = (...args) => {
                  if (String(args[1]?.method || '').toUpperCase() === 'PUT') {
                    return new Promise((_resolve, reject) => {
                      args[1]?.signal?.addEventListener(
                        'abort',
                        () => reject(new DOMException('Aborted', 'AbortError')),
                        { once: true },
                      );
                    });
                  }
                  return window.__slice1aCapacityBootstrapOriginalFetch(...args);
                };
                """
            )
            driver.execute_script(
                "arguments[0].click();",
                driver.find_element(
                    By.CSS_SELECTOR, "[data-upload-preflight-confirm]"
                ),
            )
            capacity_checkpoint_binding = json.loads(
                WebDriverWait(driver, 30).until(
                    lambda current: (
                        value
                        if (
                            value
                            := current.execute_script(
                                "return window.localStorage.getItem(arguments[0]);",
                                capacity_resume_key,
                            )
                        )
                        and json.loads(value).get("version") == 4
                        else None
                    )
                )
            )
            bench.workspace.cancel_upload_session(
                capacity_resume_matter.matter_id,
                ACTOR,
                capacity_checkpoint_binding["session_id"],
            )
            # The following fixture represents a checkpoint created before
            # receipts existed; discard only the modern bootstrap's browser key.
            driver.execute_script("window.localStorage.removeItem(arguments[0] + ':selection-receipt');", capacity_resume_key)
            driver.refresh()
            capacity_resume_input = wait.until(
                lambda current: current.find_element(
                    By.CSS_SELECTOR, "[data-file-input]"
                )
            )
            first_batch_manifest = [
                {
                    "display_name": f"record-{ordinal:05d}.txt",
                    "relative_path": f"Production/record-{ordinal:05d}.txt",
                    "media_type": "text/plain",
                    "expected_size": 128,
                }
                for ordinal in range(4)
            ]
            completed_batch, completed_items = bench.create_upload_session(
                capacity_resume_matter,
                ACTOR,
                "Synthetic completed capacity batch",
                first_batch_manifest,
            )
            capacity_store = bench.source_store(capacity_resume_matter)
            for ordinal, item in enumerate(completed_items):
                body = bytes([65 + ordinal]) * 128
                received_size = capacity_store.append_resumable_chunk(
                    item.upload_item_id,
                    offset=0,
                    expected_size=item.expected_size,
                    chunk=body,
                )
                uploaded_item = bench.workspace.set_upload_item_offset(
                    capacity_resume_matter.matter_id,
                    ACTOR,
                    completed_batch.upload_session_id,
                    item.upload_item_id,
                    0,
                    received_size,
                )
                document = capacity_store.finalize_resumable_upload(
                    item.upload_item_id,
                    display_name=uploaded_item.display_name,
                    relative_path=uploaded_item.relative_path,
                    content_type=uploaded_item.media_type,
                    expected_size=uploaded_item.expected_size,
                )
                bench.workspace.finish_upload_item(
                    capacity_resume_matter.matter_id,
                    ACTOR,
                    completed_batch.upload_session_id,
                    item.upload_item_id,
                    document.document_id,
                    queue_ingestion=False,
                )
                capacity_store.discard_resumable_upload(item.upload_item_id)
            completed_batch, _ = bench.workspace.upload_session(
                capacity_resume_matter.matter_id,
                ACTOR,
                completed_batch.upload_session_id,
            )
            open_batch, _ = bench.create_upload_session(
                capacity_resume_matter,
                ACTOR,
                "Synthetic open capacity batch",
                [
                    {
                        "display_name": "record-00004.txt",
                        "relative_path": "Production/record-00004.txt",
                        "media_type": "text/plain",
                        "expected_size": 128,
                    }
                ],
                collection_id=completed_batch.collection_id,
            )
            for filler_batch in range(7):
                filler_count = 4 if filler_batch < 6 else 3
                bench.create_upload_session(
                    capacity_resume_matter,
                    ACTOR,
                    f"Synthetic capacity reservation {filler_batch + 1}",
                    [
                        {
                            "display_name": (
                                f"reserved-{filler_batch:02d}-{ordinal:02d}.txt"
                            ),
                            "relative_path": (
                                "Reserved/"
                                f"reserved-{filler_batch:02d}-{ordinal:02d}.txt"
                            ),
                            "media_type": "text/plain",
                            "expected_size": 128,
                        }
                        for ordinal in range(filler_count)
                    ],
                )
            capacity_sessions_before = bench.workspace.recent_upload_sessions(
                capacity_resume_matter.matter_id, ACTOR, limit=20
            )
            _require(
                completed_batch.state == "complete"
                and open_batch.state == "open"
                and bench.storage.matter_payload_usage_bytes(
                    capacity_resume_matter.matter_id
                )
                == 512
                and bench.workspace.pending_upload_bytes(
                    capacity_resume_matter.matter_id
                )
                == 3_584,
                "capacity-bound resume fixture did not reach its frozen near-quota state",
            )
            capacity_checkpoint = {
                **capacity_checkpoint_binding,
                "session_id": open_batch.upload_session_id,
                "collection_id": open_batch.collection_id,
                "batch_index": 1,
            }
            capacity_panel = driver.find_element(
                By.CSS_SELECTOR, "[data-upload-preflight]"
            )
            capacity_legacy_checkpoint = {
                "version": 3,
                "plan_fingerprint": capacity_checkpoint["plan_fingerprint"],
                "session_id": open_batch.upload_session_id,
                "collection_id": open_batch.collection_id,
                "batch_index": 1,
            }
            driver.execute_script(
                "window.localStorage.setItem(arguments[0], arguments[1]);",
                capacity_resume_key,
                json.dumps(capacity_legacy_checkpoint),
            )
            _synthetic_selection(
                driver,
                capacity_resume_input,
                5,
                long_paths=False,
                cross_boundary_duplicate=False,
                file_size=128,
            )
            wait.until(
                lambda current: capacity_panel.get_attribute("aria-busy") is None
                and current.find_element(
                    By.CSS_SELECTOR, "[data-upload-preflight-state]"
                ).text
                == "Selection review paused"
            )
            _require(
                bench.workspace.upload_session(
                    capacity_resume_matter.matter_id,
                    ACTOR,
                    open_batch.upload_session_id,
                )[0].state
                == "open"
                and len(
                    bench.workspace.recent_upload_sessions(
                        capacity_resume_matter.matter_id, ACTOR, limit=20
                    )
                )
                == len(capacity_sessions_before),
                "legacy later-batch checkpoint changed its plan or server state",
            )
            driver.execute_script(
                "window.localStorage.setItem(arguments[0], arguments[1]);",
                capacity_resume_key,
                json.dumps(capacity_checkpoint),
            )
            _synthetic_selection(
                driver,
                capacity_resume_input,
                5,
                long_paths=False,
                cross_boundary_duplicate=False,
                file_size=128,
            )
            wait.until(
                lambda current: capacity_panel.get_attribute("aria-busy") is None
                and len(
                    current.find_elements(
                        By.CSS_SELECTOR, "[data-upload-preflight-items] > li"
                    )
                )
                == 5
            )
            capacity_preview_label = driver.find_element(
                By.CSS_SELECTOR, "[data-upload-preflight-confirm]"
            ).text
            driver.execute_script(
                """
                window.__slice1aCapacityResumeOriginalFetch = window.fetch;
                window.__slice1aCapacityResumeEvents = [];
                window.__slice1aCapacityResumeBodies = [];
                const checkpointSessionId = arguments[0];
                window.fetch = (...args) => {
                  const url = String(args[0]);
                  const method = String(args[1]?.method || 'GET').toUpperCase();
                  if (url.includes(`/upload-sessions/${checkpointSessionId}`) && method === 'GET') {
                    window.__slice1aCapacityResumeEvents.push({ kind: 'status' });
                  } else if (url.includes(`/upload-sessions/${checkpointSessionId}/cancel`) && method === 'POST') {
                    window.__slice1aCapacityResumeEvents.push({ kind: 'cancel' });
                  } else if (url.endsWith('/upload-sessions') && method === 'POST') {
                    window.__slice1aCapacityResumeEvents.push({ kind: 'session-post' });
                    window.__slice1aCapacityResumeBodies.push(JSON.parse(String(args[1].body)));
                  } else if (method === 'PUT') {
                    window.__slice1aCapacityResumeEvents.push({ kind: 'put' });
                    return new Promise((_resolve, reject) => {
                      args[1]?.signal?.addEventListener(
                        'abort',
                        () => reject(new DOMException('Aborted', 'AbortError')),
                        { once: true },
                      );
                    });
                  }
                  return window.__slice1aCapacityResumeOriginalFetch(...args);
                };
                """,
                open_batch.upload_session_id,
            )
            driver.execute_script(
                "arguments[0].click();",
                driver.find_element(
                    By.CSS_SELECTOR, "[data-upload-preflight-confirm]"
                ),
            )
            capacity_resume_events = WebDriverWait(driver, 30).until(
                lambda current: current.execute_script(
                    """
                    const events = window.__slice1aCapacityResumeEvents;
                    return events.some((event) => event.kind === 'put')
                      ? events
                      : null;
                    """
                )
            )
            capacity_resume_bodies = driver.execute_script(
                "return window.__slice1aCapacityResumeBodies;"
            )
            capacity_saved = json.loads(
                driver.execute_script(
                    "return window.localStorage.getItem(arguments[0]);",
                    capacity_resume_key,
                )
            )
            driver.execute_script(
                """
                window.fetch = window.__slice1aCapacityResumeOriginalFetch;
                delete window.__slice1aCapacityResumeOriginalFetch;
                """
            )
            capacity_open_after, _ = bench.workspace.upload_session(
                capacity_resume_matter.matter_id,
                ACTOR,
                open_batch.upload_session_id,
            )
            capacity_completed_after, _ = bench.workspace.upload_session(
                capacity_resume_matter.matter_id,
                ACTOR,
                completed_batch.upload_session_id,
            )
            capacity_sessions_after = bench.workspace.recent_upload_sessions(
                capacity_resume_matter.matter_id, ACTOR, limit=20
            )
            capacity_documents = tuple(
                document.relative_path
                for document in capacity_store.documents.values()
            )
            _require(
                capacity_preview_label == "Upload 5 ready files"
                and capacity_resume_events
                == [{"kind": "session-post"}, {"kind": "put"}]
                and len(capacity_resume_bodies) == 1
                and capacity_resume_bodies[0]["resume_session_id"]
                == open_batch.upload_session_id
                and capacity_resume_bodies[0]["collection_id"]
                == open_batch.collection_id
                and [
                    item["relative_path"]
                    for item in capacity_resume_bodies[0]["files"]
                ]
                == ["Production/record-00004.txt"]
                and capacity_saved == capacity_checkpoint
                and capacity_open_after.state == "open"
                and capacity_completed_after.state == "complete"
                and len(capacity_sessions_after) == len(capacity_sessions_before)
                and set(capacity_documents)
                == {
                    f"Production/record-{ordinal:05d}.txt"
                    for ordinal in range(4)
                },
                (
                    "capacity accounting restarted an exact multi-batch resume "
                    f"(preview={capacity_preview_label!r}, "
                    f"events={capacity_resume_events!r}, "
                    f"bodies={capacity_resume_bodies!r}, saved={capacity_saved!r}, "
                    f"open_state={capacity_open_after.state!r}, "
                    f"completed_state={capacity_completed_after.state!r}, "
                    f"sessions={len(capacity_sessions_after)}/"
                    f"{len(capacity_sessions_before)}, documents={capacity_documents!r})"
                ),
            )

            navigate_to(driver, f"{base_url}/matters/{slug}/setup", diagnostics=args.output, page_load_timeout=20, readiness_timeout=20)
            file_input = wait.until(
                lambda current: current.find_element(
                    By.CSS_SELECTOR, "[data-file-input]"
                )
            )
            panel = driver.find_element(By.CSS_SELECTOR, "[data-upload-preflight]")
            if "assistant-collapsed" not in driver.find_element(
                By.TAG_NAME, "body"
            ).get_attribute("class").split():
                driver.find_element(
                    By.CSS_SELECTOR, "[data-assistant-collapse]"
                ).click()
                wait.until(
                    lambda current: "assistant-collapsed"
                    in current.find_element(By.TAG_NAME, "body")
                    .get_attribute("class")
                    .split()
                )
            _network_urls(driver)

            status_calls_before_preview = scanner.status_calls
            driver.execute_script("arguments[0].focus({preventScroll: true});", file_input)
            _replace_selection(driver, file_input, files)
            panel = wait.until(
                lambda current: current.find_element(By.CSS_SELECTOR, "[data-upload-preflight]:not([hidden])")
            )
            wait.until(
                lambda current: "2 of 5 selected files can proceed"
                in current.find_element(By.CSS_SELECTOR, "[data-upload-preflight-status]").text
            )
            rows = driver.find_elements(By.CSS_SELECTOR, "[data-upload-preflight-items] > li")
            states = [row.get_attribute("data-state") for row in rows]
            _require(
                states == ["valid", "valid", "needs_attention", "unsupported", "over_limit"],
                f"unexpected preflight states: {states}",
            )
            count_text = driver.find_element(By.CSS_SELECTOR, "[data-upload-preflight-counts]").text
            for expected in (
                "2 ready",
                "1 need attention",
                "1 unsupported",
                "0 repeated paths",
                "1 over limit",
                "0 cannot use",
            ):
                _require(expected in count_text, f"missing count: {expected}")
            panel_text = panel.text
            for expected in (
                "Expected from filename",
                "Detected type, readability, source version, and content duplicates pending upload",
                "Security scan required, unavailable, and not run",
            ):
                _require(expected in panel_text, f"missing truthful preview text: {expected}")
            confirm = driver.find_element(By.CSS_SELECTOR, "[data-upload-preflight-confirm]")
            _require(confirm.text == "Upload 2 ready files", "confirmation count is wrong")
            _require(confirm.is_enabled(), "confirmation should be enabled")

            preconfirm_urls = _network_urls(driver)
            _require(any("/upload-preflight" in url for url in preconfirm_urls), "browser did not call preflight API")
            _require(not any("/upload-sessions" in url for url in preconfirm_urls), "upload session began before confirmation")
            _require(bench.workspace.source_collections(matter.matter_id) == (), "preflight created a collection")
            _require(bench.workspace.recent_upload_sessions(matter.matter_id, ACTOR) == (), "preflight created a session")
            _require(bench.workspace.pending_upload_bytes(matter.matter_id) == 0, "preflight reserved upload bytes")
            _require(
                scanner.status_calls == status_calls_before_preview + 1,
                "preflight did not perform exactly one capability-status check",
            )
            _require(scanner.scan_calls == 0, "preflight invoked a content scan")
            wait.until(
                lambda current: current.execute_script(
                    """
                    const panel = arguments[0];
                    const header = document.querySelector('.topbar');
                    const panelRect = panel.getBoundingClientRect();
                    const headerBottom = header ? header.getBoundingClientRect().bottom : 0;
                    return panelRect.top >= headerBottom
                      && panelRect.top < window.innerHeight
                      && panelRect.bottom > headerBottom;
                    """,
                    panel,
                )
            )
            _require(
                driver.execute_script(
                    "return document.activeElement === arguments[0];", file_input
                ),
                "completed preflight stole focus from the selection control",
            )
            desktop_overflow = driver.execute_script(
                "return document.documentElement.scrollWidth - document.documentElement.clientWidth;"
            )
            _require(
                desktop_overflow <= 0,
                f"desktop page overflows horizontally by {desktop_overflow}px",
            )
            driver.save_screenshot(str(output / "desktop-preflight.png"))

            _replace_selection(driver, file_input, [files[0]])
            wait.until(
                lambda current: len(
                    current.find_elements(By.CSS_SELECTOR, "[data-upload-preflight-items] > li")
                )
                == 1
                and current.find_element(By.CSS_SELECTOR, "[data-upload-preflight-confirm]").text
                == "Upload 1 ready file"
            )
            _replace_selection(driver, file_input, files)
            wait.until(
                lambda current: len(
                    current.find_elements(By.CSS_SELECTOR, "[data-upload-preflight-items] > li")
                )
                == 5
                and current.find_element(By.CSS_SELECTOR, "[data-upload-preflight-confirm]").text
                == "Upload 2 ready files"
            )

            driver.execute_script(
                """
                window.__slice1aOriginalFetch = window.fetch;
                window.fetch = (...args) => String(args[0]).includes('/upload-preflight')
                  ? Promise.reject(new Error('Synthetic preflight interruption'))
                  : window.__slice1aOriginalFetch(...args);
                """
            )
            _replace_selection(driver, file_input, [files[0]])
            wait.until(
                lambda current: current.find_element(
                    By.CSS_SELECTOR, "[data-upload-preflight-state]"
                ).text
                == "Selection review paused"
            )
            _require(
                not driver.find_elements(
                    By.CSS_SELECTOR, "[data-upload-preflight-items] > li"
                ),
                "failed reselection retained stale rows",
            )
            _require(
                not driver.find_elements(
                    By.CSS_SELECTOR, "[data-upload-preflight-counts] > span"
                ),
                "failed reselection retained stale totals",
            )
            _require(
                not driver.find_element(
                    By.CSS_SELECTOR, "[data-upload-preflight-confirm]"
                ).is_enabled(),
                "failed reselection left confirmation enabled",
            )
            _require(
                panel.get_attribute("aria-busy") is None,
                "failed reselection left the panel busy",
            )
            driver.execute_script(
                "window.fetch = window.__slice1aOriginalFetch; delete window.__slice1aOriginalFetch;"
            )

            driver.execute_script(
                """
                window.__slice1aOriginalFetch = window.fetch;
                window.__slice1aPendingFetch = false;
                window.fetch = (...args) => {
                  if (!String(args[0]).includes('/upload-preflight')) {
                    return window.__slice1aOriginalFetch(...args);
                  }
                  window.__slice1aPendingFetch = true;
                  return new Promise((_resolve, reject) => {
                    const signal = args[1] && args[1].signal;
                    if (signal) {
                      signal.addEventListener(
                        'abort',
                        () => reject(new DOMException('Aborted', 'AbortError')),
                        { once: true },
                      );
                    }
                  });
                };
                """
            )
            _replace_selection(driver, file_input, [files[0]])
            wait.until(
                lambda current: current.execute_script(
                    "return window.__slice1aPendingFetch === true;"
                )
                and panel.get_attribute("aria-busy") == "true"
            )
            driver.execute_script(
                """
                arguments[0].value = '';
                arguments[0].dispatchEvent(new Event('change', { bubbles: true }));
                """,
                file_input,
            )
            wait.until(
                lambda current: panel.get_attribute("aria-busy") is None
                and current.find_element(
                    By.CSS_SELECTOR, "[data-upload-preflight-state]"
                ).text
                == "Waiting for a selection"
            )
            _require(
                not driver.find_elements(
                    By.CSS_SELECTOR, "[data-upload-preflight-items] > li"
                ),
                "clearing an in-flight selection retained stale rows",
            )
            _require(
                not driver.find_elements(
                    By.CSS_SELECTOR, "[data-upload-preflight-counts] > span"
                ),
                "clearing an in-flight selection retained stale totals",
            )
            _require(
                not driver.find_element(
                    By.CSS_SELECTOR, "[data-upload-preflight-confirm]"
                ).is_enabled(),
                "clearing an in-flight selection left confirmation enabled",
            )
            driver.execute_script(
                """
                window.fetch = window.__slice1aOriginalFetch;
                delete window.__slice1aOriginalFetch;
                delete window.__slice1aPendingFetch;
                """
            )
            _replace_selection(driver, file_input, files)
            wait.until(
                lambda current: len(
                    current.find_elements(By.CSS_SELECTOR, "[data-upload-preflight-items] > li")
                )
                == 5
                and current.find_element(By.CSS_SELECTOR, "[data-upload-preflight-confirm]").text
                == "Upload 2 ready files"
            )

            driver.set_window_size(390, 844)
            wait.until(lambda current: current.execute_script("return window.innerWidth") <= 390)
            wait.until(
                lambda current: current.execute_script(
                    "return document.querySelector('.matter-rail').getBoundingClientRect().right;"
                )
                <= 1
            )
            overflow = driver.execute_script(
                "return document.documentElement.scrollWidth - document.documentElement.clientWidth;"
            )
            _require(overflow <= 0, f"mobile page overflows horizontally by {overflow}px")
            confirm = driver.find_element(By.CSS_SELECTOR, "[data-upload-preflight-confirm]")
            driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", confirm)
            driver.save_screenshot(str(output / "mobile-preflight.png"))
            confirm.send_keys(Keys.ENTER)
            _wait_until(
                lambda: bench.workspace.recent_upload_sessions(matter.matter_id, ACTOR),
                "confirmation did not create an upload session",
            )
            session = _wait_until(
                lambda: (
                    candidate
                    if (candidate := bench.workspace.recent_upload_sessions(matter.matter_id, ACTOR)[0]).state
                    in {"complete", "partial"}
                    else None
                ),
                "retained upload path did not reach a terminal session",
            )
            _require(session.item_count == 2, "ineligible files entered the upload session")
            _require(session.state == "partial", "malformed synthetic PDF did not remain visible as partial")
            _, upload_items = bench.workspace.upload_session(
                matter.matter_id, ACTOR, session.upload_session_id
            )
            _require(
                [item.state for item in upload_items] == ["queued", "failed"],
                f"unexpected retained upload states: {[item.state for item in upload_items]}",
            )
            _require(scanner.scan_calls == 0, "excluded scanner-gated file received a content scan")
            _require(
                len(driver.find_elements(By.CSS_SELECTOR, "[data-upload-preflight-items] > li")) == 5,
                "ineligible preview rows disappeared after confirmation",
            )
            progress = wait.until(
                lambda current: (
                    candidate
                    if (candidate := current.find_element(By.CSS_SELECTOR, "[data-upload-progress]")).is_displayed()
                    else None
                )
            )
            postconfirm_urls = _network_urls(driver)
            _require(any("/upload-sessions" in url for url in postconfirm_urls), "browser did not use retained upload API")
            driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", progress)
            driver.save_screenshot(str(output / "mobile-partial-upload.png"))

            driver.set_window_size(1536, 1024)
            terminal_matter = bench.create_matter(
                "Synthetic terminal checkpoint matter",
                "Generated changed-plan terminal recovery acceptance",
                ACTOR,
            )
            terminal_key = f"case-intelligence:upload:{terminal_matter.slug}"
            navigate_to(driver, f"{base_url}/matters/{terminal_matter.slug}/setup", diagnostics=args.output, page_load_timeout=20, readiness_timeout=20)
            terminal_input = wait.until(
                lambda current: current.find_element(
                    By.CSS_SELECTOR, "[data-file-input]"
                )
            )
            _select(terminal_input, [files[0]])
            wait.until(
                lambda current: current.find_element(
                    By.CSS_SELECTOR, "[data-upload-preflight-confirm]"
                ).text
                == "Upload 1 ready file"
            )
            terminal_old_fingerprint = _upload_plan_fingerprint(
                driver, terminal_input
            )
            driver.execute_script(
                "arguments[0].click();",
                driver.find_element(
                    By.CSS_SELECTOR, "[data-upload-preflight-confirm]"
                ),
            )
            terminal_session = _wait_until(
                lambda: (
                    candidate
                    if (
                        sessions := bench.workspace.recent_upload_sessions(
                            terminal_matter.matter_id, ACTOR
                        )
                    )
                    and (candidate := sessions[0]).state == "complete"
                    else None
                ),
                "synthetic completed checkpoint session did not finish",
            )
            wait.until(
                lambda current: current.find_element(
                    By.CSS_SELECTOR, "[data-upload-form]"
                ).get_attribute("aria-busy")
                is None
            )
            _require(
                bench.workspace.pending_upload_bytes(terminal_matter.matter_id) == 0,
                "completed checkpoint retained pending bytes",
            )
            terminal_checkpoint = json.dumps(
                {
                    "version": 3,
                    "plan_fingerprint": terminal_old_fingerprint,
                    "session_id": terminal_session.upload_session_id,
                    "collection_id": terminal_session.collection_id,
                    "batch_index": 0,
                }
            )
            driver.execute_script(
                "window.localStorage.setItem(arguments[0], arguments[1]);",
                terminal_key,
                terminal_checkpoint,
            )
            _replace_selection(driver, terminal_input, [files[0], files[1]])
            wait.until(
                lambda current: current.find_element(
                    By.CSS_SELECTOR, "[data-upload-preflight-confirm]"
                ).text
                == "Upload 2 ready files"
            )
            terminal_sessions_before = len(
                bench.workspace.recent_upload_sessions(
                    terminal_matter.matter_id, ACTOR
                )
            )
            driver.execute_script(
                """
                window.__slice1aTerminalOriginalFetch = window.fetch;
                window.__slice1aTerminalEvents = [];
                window.__slice1aTerminalBodies = [];
                const staleSessionId = arguments[0];
                window.fetch = (...args) => {
                  const url = String(args[0]);
                  const method = String(args[1]?.method || 'GET').toUpperCase();
                  if (url.includes(`/upload-sessions/${staleSessionId}`) && method === 'GET') {
                    window.__slice1aTerminalEvents.push({ kind: 'status' });
                  } else if (url.includes(`/upload-sessions/${staleSessionId}/cancel`) && method === 'POST') {
                    window.__slice1aTerminalEvents.push({ kind: 'cancel' });
                  } else if (url.endsWith('/upload-sessions') && method === 'POST') {
                    window.__slice1aTerminalEvents.push({ kind: 'create' });
                    window.__slice1aTerminalBodies.push(JSON.parse(String(args[1].body)));
                  }
                  if (method === 'PUT') {
                    return new Promise((_resolve, reject) => {
                      args[1]?.signal?.addEventListener(
                        'abort',
                        () => reject(new DOMException('Aborted', 'AbortError')),
                        { once: true },
                      );
                    });
                  }
                  return window.__slice1aTerminalOriginalFetch(...args);
                };
                """,
                terminal_session.upload_session_id,
            )
            driver.execute_script(
                "arguments[0].click();",
                driver.find_element(
                    By.CSS_SELECTOR, "[data-upload-preflight-confirm]"
                ),
            )
            terminal_events = WebDriverWait(driver, 30).until(
                lambda current: (
                    events
                    if (
                        events
                        := current.execute_script(
                            "return window.__slice1aTerminalEvents;"
                        )
                    )
                    and current.execute_script(
                        """
                        const stored = window.localStorage.getItem(arguments[0]);
                        if (!stored) return false;
                        try {
                          const parsed = JSON.parse(stored);
                          return parsed.session_id && parsed.session_id !== arguments[1];
                        } catch (_error) {
                          return false;
                        }
                        """,
                        terminal_key,
                        terminal_session.upload_session_id,
                    )
                    else None
                )
            )
            terminal_bodies = driver.execute_script(
                "return window.__slice1aTerminalBodies;"
            )
            terminal_saved = driver.execute_script(
                "return window.localStorage.getItem(arguments[0]);", terminal_key
            )
            driver.execute_script(
                """
                window.fetch = window.__slice1aTerminalOriginalFetch;
                delete window.__slice1aTerminalOriginalFetch;
                """
            )
            terminal_current_sessions = bench.workspace.recent_upload_sessions(
                terminal_matter.matter_id, ACTOR
            )
            terminal_pending = bench.workspace.pending_upload_bytes(
                terminal_matter.matter_id
            )
            terminal_expected_pending = sum(
                path.stat().st_size for path in files[:2]
            )
            terminal_original_state = bench.workspace.upload_session(
                terminal_matter.matter_id,
                ACTOR,
                terminal_session.upload_session_id,
            )[0].state
            _require(
                terminal_events == [{"kind": "status"}, {"kind": "create"}]
                and len(terminal_bodies) == 1
                and terminal_bodies[0]["resume_session_id"] == ""
                and terminal_bodies[0]["collection_id"] == ""
                and terminal_saved is not None
                and json.loads(terminal_saved)["session_id"]
                != terminal_session.upload_session_id
                and terminal_original_state == "complete"
                and len(terminal_current_sessions) == terminal_sessions_before + 1
                and terminal_pending == terminal_expected_pending,
                (
                    "completed stale checkpoint trapped changed-plan recovery "
                    f"(events={terminal_events!r}, bodies={terminal_bodies!r}, "
                    f"saved={terminal_saved!r}, original_state={terminal_original_state!r}, "
                    f"sessions={len(terminal_current_sessions)}/{terminal_sessions_before + 1}, "
                    f"pending={terminal_pending}/{terminal_expected_pending})"
                ),
            )

            terminal_new_state = json.loads(terminal_saved)
            driver.refresh()
            bench.workspace.cancel_upload_session(
                terminal_matter.matter_id,
                ACTOR,
                terminal_new_state["session_id"],
            )
            driver.execute_script(
                "window.localStorage.removeItem(arguments[0]);", terminal_key
            )

            partial_stale, partial_stale_items = bench.create_upload_session(
                terminal_matter,
                ACTOR,
                "Synthetic terminal partial checkpoint",
                [
                    {
                        "display_name": "partial.txt",
                        "relative_path": "terminal/partial.txt",
                        "media_type": "text/plain",
                        "expected_size": 12,
                    }
                ],
            )
            bench.workspace.fail_upload_item(
                terminal_matter.matter_id,
                ACTOR,
                partial_stale.upload_session_id,
                partial_stale_items[0].upload_item_id,
                "Synthetic terminal partial state",
            )
            partial_stale, _ = bench.workspace.upload_session(
                terminal_matter.matter_id, ACTOR, partial_stale.upload_session_id
            )
            cancelled_stale, _ = bench.create_upload_session(
                terminal_matter,
                ACTOR,
                "Synthetic terminal cancelled checkpoint",
                [
                    {
                        "display_name": "cancelled.txt",
                        "relative_path": "terminal/cancelled.txt",
                        "media_type": "text/plain",
                        "expected_size": 12,
                    }
                ],
            )
            bench.workspace.cancel_upload_session(
                terminal_matter.matter_id,
                ACTOR,
                cancelled_stale.upload_session_id,
            )
            cancelled_stale, _ = bench.workspace.upload_session(
                terminal_matter.matter_id,
                ACTOR,
                cancelled_stale.upload_session_id,
            )
            _require(
                partial_stale.state == "partial"
                and cancelled_stale.state == "cancelled",
                "terminal status fixtures were not prepared",
            )

            def exercise_stale_status(
                label: str,
                session_id: str,
                collection_id: str,
                response_mode: str,
                *,
                should_release: bool,
            ) -> None:
                navigate_to(driver, f"{base_url}/matters/{terminal_matter.slug}/setup", diagnostics=args.output, page_load_timeout=20, readiness_timeout=20)
                checkpoint = json.dumps(
                    {
                        "version": 3,
                        "plan_fingerprint": terminal_old_fingerprint,
                        "session_id": session_id,
                        "collection_id": collection_id,
                        "batch_index": 0,
                    }
                )
                # This fixture represents a pre-receipt browser checkpoint.
                # Earlier modern uploads in this matter must not donate their
                # already-bound selection receipt to an unrelated legacy retry.
                driver.execute_script(
                    "window.localStorage.removeItem(arguments[0] + ':selection-receipt');"
                    "window.localStorage.setItem(arguments[0], arguments[1]);",
                    terminal_key,
                    checkpoint,
                )
                status_input = driver.find_element(
                    By.CSS_SELECTOR, "[data-file-input]"
                )
                _select(status_input, [files[0], files[1]])
                wait.until(
                    lambda current: current.find_element(
                        By.CSS_SELECTOR, "[data-upload-preflight-confirm]"
                    ).text
                    == "Upload 2 ready files"
                )
                pending_before = bench.workspace.pending_upload_bytes(
                    terminal_matter.matter_id
                )
                driver.execute_script(
                    """
                    window.__slice1aStatusOriginalFetch = window.fetch;
                    window.__slice1aStatusEvents = [];
                    window.__slice1aStatusBodies = [];
                    const staleSessionId = arguments[0];
                    const staleCollectionId = arguments[1];
                    const mode = arguments[2];
                    const jsonResponse = (payload, status = 200) => Promise.resolve(
                      new Response(JSON.stringify(payload), {
                        status,
                        headers: { 'Content-Type': 'application/json' },
                      })
                    );
                    window.fetch = (...args) => {
                      const url = String(args[0]);
                      const method = String(args[1]?.method || 'GET').toUpperCase();
                      if (url.includes(`/upload-sessions/${staleSessionId}`) && method === 'GET') {
                        window.__slice1aStatusEvents.push({ kind: 'status' });
                        if (mode === 'network') return Promise.reject(new TypeError('Synthetic status network failure'));
                        if (mode === 'server') return jsonResponse({ message: 'Synthetic status unavailable.' }, 503);
                        if (mode === 'malformed') return jsonResponse({ state: 'cancelled' });
                        if (mode === 'wrong-session') return jsonResponse({
                          upload_session_id: 'upload-session-eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee',
                          collection_id: staleCollectionId,
                          state: 'cancelled',
                        });
                        if (mode === 'wrong-collection') return jsonResponse({
                          upload_session_id: staleSessionId,
                          collection_id: 'source-collection-eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee',
                          state: 'cancelled',
                        });
                      } else if (url.includes(`/upload-sessions/${staleSessionId}/cancel`) && method === 'POST') {
                        window.__slice1aStatusEvents.push({ kind: 'cancel' });
                      } else if (url.endsWith('/upload-sessions') && method === 'POST') {
                        window.__slice1aStatusEvents.push({ kind: 'create' });
                        window.__slice1aStatusBodies.push(JSON.parse(String(args[1].body)));
                      }
                      if (method === 'PUT') {
                        return new Promise((_resolve, reject) => {
                          args[1]?.signal?.addEventListener(
                            'abort',
                            () => reject(new DOMException('Aborted', 'AbortError')),
                            { once: true },
                          );
                        });
                      }
                      return window.__slice1aStatusOriginalFetch(...args);
                    };
                    """,
                    session_id,
                    collection_id,
                    response_mode,
                )
                driver.execute_script(
                    "arguments[0].click();",
                    driver.find_element(
                        By.CSS_SELECTOR, "[data-upload-preflight-confirm]"
                    ),
                )
                if should_release:
                    WebDriverWait(driver, 30).until(
                        lambda current: current.execute_script(
                            """
                            const saved = window.localStorage.getItem(arguments[0]);
                            if (!saved || window.__slice1aStatusBodies.length !== 1) return false;
                            return JSON.parse(saved).session_id !== arguments[1];
                            """,
                            terminal_key,
                            session_id,
                        )
                    )
                else:
                    WebDriverWait(driver, 30).until(
                        lambda current: current.execute_script(
                            "return window.__slice1aStatusEvents.some(event => event.kind === 'status');"
                        ) and current.find_element(
                            By.CSS_SELECTOR, "[data-upload-form]"
                        ).get_attribute("aria-busy") is None
                    )
                events = driver.execute_script(
                    "return window.__slice1aStatusEvents;"
                )
                bodies = driver.execute_script(
                    "return window.__slice1aStatusBodies;"
                )
                checkpoint_after = driver.execute_script(
                    "return window.localStorage.getItem(arguments[0]);", terminal_key
                )
                driver.execute_script(
                    """
                    window.fetch = window.__slice1aStatusOriginalFetch;
                    delete window.__slice1aStatusOriginalFetch;
                    """
                )
                if should_release:
                    saved_state = json.loads(checkpoint_after or "{}")
                    try:
                        created_session, _ = bench.workspace.upload_session(
                            terminal_matter.matter_id,
                            ACTOR,
                            str(saved_state.get("session_id") or ""),
                        )
                    except KeyError:
                        created_session = None
                    _require(
                        events == [{"kind": "status"}, {"kind": "create"}]
                        and len(bodies) == 1
                        and bodies[0]["resume_session_id"] == ""
                        and bodies[0]["collection_id"] == ""
                        and checkpoint_after is not None
                        and saved_state["session_id"] != session_id
                        and created_session is not None
                        and created_session.collection_id
                        == saved_state["collection_id"]
                        and created_session.state == "open"
                        and bench.workspace.pending_upload_bytes(
                            terminal_matter.matter_id
                        )
                        == pending_before
                        + sum(path.stat().st_size for path in files[:2]),
                        (
                            f"{label} stale checkpoint did not recover from batch zero "
                            f"(events={events!r}, bodies={len(bodies)}, "
                            f"checkpoint_changed={checkpoint_after != checkpoint}, "
                            f"created_state={getattr(created_session, 'state', None)!r}, "
                            f"collection_match={bool(created_session) and created_session.collection_id == saved_state.get('collection_id')}, "
                            f"fresh_body={bool(bodies) and bodies[0].get('resume_session_id') == '' and bodies[0].get('collection_id') == ''}, "
                            f"pending={bench.workspace.pending_upload_bytes(terminal_matter.matter_id) - pending_before})"
                        ),
                    )
                    new_session_id = json.loads(checkpoint_after)["session_id"]
                    driver.refresh()
                    bench.workspace.cancel_upload_session(
                        terminal_matter.matter_id, ACTOR, new_session_id
                    )
                    driver.execute_script(
                        "window.localStorage.removeItem(arguments[0]);", terminal_key
                    )
                else:
                    _require(
                        events == [{"kind": "status"}]
                        and bodies == []
                        and checkpoint_after == checkpoint
                        and bench.workspace.pending_upload_bytes(
                            terminal_matter.matter_id
                        )
                        == pending_before,
                        f"{label} status failure cleared recovery state or started work",
                    )

            for terminal_label, terminal_state in (
                ("partial", partial_stale),
                ("cancelled", cancelled_stale),
            ):
                exercise_stale_status(
                    terminal_label,
                    terminal_state.upload_session_id,
                    terminal_state.collection_id,
                    "actual",
                    should_release=True,
                )
            exercise_stale_status(
                "missing",
                "upload-session-dddddddddddddddddddddddddddddddd",
                "source-collection-dddddddddddddddddddddddddddddddd",
                "actual",
                should_release=True,
            )
            for failure_index, failure_mode in enumerate(
                (
                    "network",
                    "server",
                    "malformed",
                    "wrong-session",
                    "wrong-collection",
                ),
                start=1,
            ):
                exercise_stale_status(
                    failure_mode,
                    f"upload-session-{failure_index:032x}",
                    f"source-collection-{failure_index:032x}",
                    failure_mode,
                    should_release=False,
                )

            resume_matter = bench.create_matter(
                "Synthetic cross-refresh resume matter",
                "Generated exact-plan resume acceptance",
                ACTOR,
            )
            resume_key = f"case-intelligence:upload:{resume_matter.slug}"
            navigate_to(driver, f"{base_url}/matters/{resume_matter.slug}/setup", diagnostics=args.output, page_load_timeout=20, readiness_timeout=20)
            resume_input = wait.until(
                lambda current: current.find_element(
                    By.CSS_SELECTOR, "[data-file-input]"
                )
            )
            _select(resume_input, [files[0]])
            wait.until(
                lambda current: current.find_element(
                    By.CSS_SELECTOR, "[data-upload-preflight-confirm]"
                ).text
                == "Upload 1 ready file"
            )
            driver.execute_script(
                """
                window.__slice1aResumeOriginalFetch = window.fetch;
                window.fetch = (...args) => {
                  if (String(args[1]?.method || '').toUpperCase() === 'PUT') {
                    return new Promise((_resolve, reject) => {
                      args[1]?.signal?.addEventListener(
                        'abort',
                        () => reject(new DOMException('Aborted', 'AbortError')),
                        { once: true },
                      );
                    });
                  }
                  return window.__slice1aResumeOriginalFetch(...args);
                };
                """
            )
            driver.execute_script(
                "arguments[0].click();",
                driver.find_element(
                    By.CSS_SELECTOR, "[data-upload-preflight-confirm]"
                ),
            )
            saved_resume = WebDriverWait(driver, 30).until(
                lambda current: current.execute_script(
                    "return window.localStorage.getItem(arguments[0]);", resume_key
                )
            )
            saved_resume_state = json.loads(saved_resume)
            _require(saved_resume_state["version"] == 4, "resume state was not v4")
            _require(
                len(saved_resume_state["raw_selection_fingerprint"]) == 64
                and len(saved_resume_state["structure_fingerprint"]) == 64
                and len(saved_resume_state["plan_fingerprint"]) == 64
                and saved_resume_state["eligible_indexes"] == [0]
                and saved_resume_state["batch_count"] == 1
                and saved_resume_state["batch_index"] == 0,
                "resume state did not bind the exact reviewed upload plan",
            )
            original_resume_session = saved_resume_state["session_id"]
            original_resume_collection = saved_resume_state["collection_id"]

            driver.refresh()
            resume_input = wait.until(
                lambda current: current.find_element(
                    By.CSS_SELECTOR, "[data-file-input]"
                )
            )
            _select(resume_input, [files[0]])
            wait.until(
                lambda current: current.find_element(
                    By.CSS_SELECTOR, "[data-upload-preflight-confirm]"
                ).text
                == "Upload 1 ready file"
            )
            driver.execute_script(
                """
                window.__slice1aResumeOriginalFetch = window.fetch;
                window.__slice1aResumeBodies = [];
                window.fetch = (...args) => {
                  const method = String(args[1]?.method || 'GET').toUpperCase();
                  if (String(args[0]).includes('/upload-sessions') && method === 'POST') {
                    window.__slice1aResumeBodies.push(JSON.parse(String(args[1].body)));
                  }
                  if (method === 'PUT') {
                    return new Promise((_resolve, reject) => {
                      args[1]?.signal?.addEventListener(
                        'abort',
                        () => reject(new DOMException('Aborted', 'AbortError')),
                        { once: true },
                      );
                    });
                  }
                  return window.__slice1aResumeOriginalFetch(...args);
                };
                """
            )
            driver.execute_script(
                "arguments[0].click();",
                driver.find_element(
                    By.CSS_SELECTOR, "[data-upload-preflight-confirm]"
                ),
            )
            resumed_body = WebDriverWait(driver, 30).until(
                lambda current: (
                    bodies[0]
                    if (
                        bodies
                        := current.execute_script(
                            "return window.__slice1aResumeBodies;"
                        )
                    )
                    else None
                )
            )
            _require(
                resumed_body["resume_session_id"] == original_resume_session
                and resumed_body["collection_id"] == original_resume_collection,
                "same-subset cross-refresh did not resume the exact saved session",
            )
            _require(
                len(
                    bench.workspace.recent_upload_sessions(
                        resume_matter.matter_id, ACTOR
                    )
                )
                == 1,
                "exact resume created a second upload session",
            )

            driver.refresh()
            missing_resume_state = {
                **saved_resume_state,
                "session_id": "upload-session-ffffffffffffffffffffffffffffffff",
            }
            driver.execute_script(
                "window.localStorage.setItem(arguments[0], arguments[1]);",
                resume_key,
                json.dumps(missing_resume_state),
            )
            resume_input = driver.find_element(By.CSS_SELECTOR, "[data-file-input]")
            _select(resume_input, [files[0]])
            wait.until(
                lambda current: current.find_element(
                    By.CSS_SELECTOR, "[data-upload-preflight-confirm]"
                ).text
                == "Upload 1 ready file"
            )
            driver.execute_script(
                """
                window.__slice1aRetryOriginalFetch = window.fetch;
                window.__slice1aRetryBodies = [];
                window.fetch = (...args) => {
                  const method = String(args[1]?.method || 'GET').toUpperCase();
                  if (String(args[0]).includes('/upload-sessions') && method === 'POST') {
                    window.__slice1aRetryBodies.push(JSON.parse(String(args[1].body)));
                  }
                  if (method === 'PUT') {
                    return new Promise((_resolve, reject) => {
                      args[1]?.signal?.addEventListener(
                        'abort',
                        () => reject(new DOMException('Aborted', 'AbortError')),
                        { once: true },
                      );
                    });
                  }
                  return window.__slice1aRetryOriginalFetch(...args);
                };
                """
            )
            driver.execute_script(
                "arguments[0].click();",
                driver.find_element(
                    By.CSS_SELECTOR, "[data-upload-preflight-confirm]"
                ),
            )
            retry_bodies = WebDriverWait(driver, 30).until(
                lambda current: (
                    bodies
                    if len(
                        bodies
                        := current.execute_script(
                            "return window.__slice1aRetryBodies;"
                        )
                    ) == 1
                    and current.find_element(
                        By.CSS_SELECTOR, "[data-upload-form]"
                    ).get_attribute("aria-busy")
                    is None
                    else None
                )
            )
            preserved_missing_resume = driver.execute_script(
                "return window.localStorage.getItem(arguments[0]);", resume_key
            )
            _require(
                retry_bodies[0]["resume_session_id"]
                == missing_resume_state["session_id"]
                and retry_bodies[0]["collection_id"]
                == missing_resume_state["collection_id"]
                and preserved_missing_resume == json.dumps(missing_resume_state)
                and len(
                    bench.workspace.recent_upload_sessions(
                        resume_matter.matter_id, ACTOR
                    )
                )
                == 1,
                "v4 resume mismatch did not fail closed with its checkpoint preserved",
            )

            driver.refresh()
            driver.execute_script(
                "window.localStorage.setItem(arguments[0], arguments[1]);",
                resume_key,
                saved_resume,
            )
            changed_input = driver.find_element(By.CSS_SELECTOR, "[data-file-input]")
            _select(changed_input, [files[0], files[1]])
            wait.until(
                lambda current: current.find_element(
                    By.CSS_SELECTOR, "[data-upload-preflight-confirm]"
                ).text
                == "Upload 2 ready files"
            )
            sessions_before_changed_plan = len(
                bench.workspace.recent_upload_sessions(
                    resume_matter.matter_id, ACTOR
                )
            )
            stale_session_before, _ = bench.workspace.upload_session(
                resume_matter.matter_id, ACTOR, original_resume_session
            )
            stale_pending_before = bench.workspace.pending_upload_bytes(
                resume_matter.matter_id
            )
            _require(
                stale_session_before.state == "open"
                and stale_pending_before >= stale_session_before.total_bytes > 0,
                "changed-plan fixture was not a live reserved upload session",
            )
            driver.execute_script(
                """
                window.__slice1aChangedOriginalFetch = window.fetch;
                window.__slice1aChangedEvents = [];
                window.__slice1aChangedBodies = [];
                const staleSessionId = arguments[0];
                window.fetch = (...args) => {
                  const url = String(args[0]);
                  const method = String(args[1]?.method || 'GET').toUpperCase();
                  if (url.includes(`/upload-sessions/${staleSessionId}`) && method === 'GET') {
                    window.__slice1aChangedEvents.push({ kind: 'status' });
                  } else if (url.includes(`/upload-sessions/${staleSessionId}/cancel`) && method === 'POST') {
                    window.__slice1aChangedEvents.push({
                      kind: 'cancel',
                      body: String(args[1]?.body || ''),
                    });
                    return Promise.resolve(new Response(
                      JSON.stringify({ message: 'Synthetic cancellation unavailable.' }),
                      { status: 503, headers: { 'Content-Type': 'application/json' } },
                    ));
                  }
                  if (url.endsWith('/upload-sessions') && method === 'POST') {
                    window.__slice1aChangedEvents.push({ kind: 'create' });
                    window.__slice1aChangedBodies.push(JSON.parse(String(args[1].body)));
                  }
                  if (method === 'PUT') {
                    return new Promise((_resolve, reject) => {
                      args[1]?.signal?.addEventListener(
                        'abort',
                        () => reject(new DOMException('Aborted', 'AbortError')),
                        { once: true },
                      );
                    });
                  }
                  return window.__slice1aChangedOriginalFetch(...args);
                };
                """,
                original_resume_session,
            )
            driver.execute_script(
                "arguments[0].click();",
                driver.find_element(
                    By.CSS_SELECTOR, "[data-upload-preflight-confirm]"
                ),
            )
            failed_cancel_events = WebDriverWait(driver, 30).until(
                lambda current: (
                    events
                    if (
                        events
                        := current.execute_script(
                            "return window.__slice1aChangedEvents;"
                        )
                    )
                    and (
                        current.find_element(
                            By.CSS_SELECTOR, "[data-upload-form]"
                        ).get_attribute("aria-busy")
                        is None
                        or current.execute_script(
                            "return window.__slice1aChangedBodies.length > 0;"
                        )
                    )
                    else None
                )
            )
            failed_cancel_checkpoint = driver.execute_script(
                "return window.localStorage.getItem(arguments[0]);", resume_key
            )
            failed_cancel_bodies = driver.execute_script(
                "return window.__slice1aChangedBodies;"
            )
            driver.execute_script(
                """
                window.fetch = window.__slice1aChangedOriginalFetch;
                delete window.__slice1aChangedOriginalFetch;
                """
            )
            failed_cancel_state = bench.workspace.upload_session(
                resume_matter.matter_id, ACTOR, original_resume_session
            )[0].state
            failed_cancel_pending = bench.workspace.pending_upload_bytes(
                resume_matter.matter_id
            )
            failed_cancel_session_count = len(
                bench.workspace.recent_upload_sessions(
                    resume_matter.matter_id, ACTOR
                )
            )
            _require(
                failed_cancel_events
                == [{"kind": "status"}, {"kind": "cancel", "body": ""}]
                and failed_cancel_bodies == []
                and failed_cancel_checkpoint == saved_resume
                and failed_cancel_state == "open"
                and failed_cancel_pending == stale_pending_before
                and failed_cancel_session_count == sessions_before_changed_plan,
                (
                    "failed stale-session cancellation cleared recovery state or "
                    f"started new work (events={failed_cancel_events!r}, "
                    f"bodies={failed_cancel_bodies!r}, "
                    f"checkpoint_preserved={failed_cancel_checkpoint == saved_resume}, "
                    f"state={failed_cancel_state!r}, "
                    f"pending={failed_cancel_pending}/{stale_pending_before}, "
                    f"sessions={failed_cancel_session_count}/"
                    f"{sessions_before_changed_plan})"
                ),
            )

            _replace_selection(driver, changed_input, [files[0], files[1]])
            wait.until(
                lambda current: current.find_element(
                    By.CSS_SELECTOR, "[data-upload-preflight-confirm]"
                ).is_enabled()
                and current.find_element(
                    By.CSS_SELECTOR, "[data-upload-preflight-confirm]"
                ).text
                == "Upload 2 ready files"
            )
            driver.execute_script(
                """
                window.__slice1aChangedOriginalFetch = window.fetch;
                window.__slice1aChangedEvents = [];
                window.__slice1aChangedBodies = [];
                const staleSessionId = arguments[0];
                window.fetch = (...args) => {
                  const url = String(args[0]);
                  const method = String(args[1]?.method || 'GET').toUpperCase();
                  if (url.includes(`/upload-sessions/${staleSessionId}`) && method === 'GET') {
                    window.__slice1aChangedEvents.push({ kind: 'status' });
                  } else if (url.includes(`/upload-sessions/${staleSessionId}/cancel`) && method === 'POST') {
                    window.__slice1aChangedEvents.push({
                      kind: 'cancel',
                      body: String(args[1]?.body || ''),
                    });
                  } else if (url.endsWith('/upload-sessions') && method === 'POST') {
                    window.__slice1aChangedEvents.push({ kind: 'create' });
                    window.__slice1aChangedBodies.push(JSON.parse(String(args[1].body)));
                  }
                  if (method === 'PUT') {
                    return new Promise((_resolve, reject) => {
                      args[1]?.signal?.addEventListener(
                        'abort',
                        () => reject(new DOMException('Aborted', 'AbortError')),
                        { once: true },
                      );
                    });
                  }
                  return window.__slice1aChangedOriginalFetch(...args);
                };
                """,
                original_resume_session,
            )
            driver.execute_script(
                "arguments[0].click();",
                driver.find_element(
                    By.CSS_SELECTOR, "[data-upload-preflight-confirm]"
                ),
            )
            changed_body = WebDriverWait(driver, 30).until(
                lambda current: (
                    bodies[0]
                    if (
                        bodies
                        := current.execute_script(
                            "return window.__slice1aChangedBodies;"
                        )
                    )
                    else None
                )
            )
            changed_events = driver.execute_script(
                "return window.__slice1aChangedEvents;"
            )
            changed_saved = json.loads(
                WebDriverWait(driver, 30).until(
                    lambda current: current.execute_script(
                        "return window.localStorage.getItem(arguments[0]);",
                        resume_key,
                    )
                )
            )
            _require(
                changed_events
                == [
                    {"kind": "status"},
                    {"kind": "cancel", "body": ""},
                    {"kind": "create"},
                ]
                and changed_body["resume_session_id"] == ""
                and changed_body["collection_id"] == ""
                and changed_body["files"][0]["relative_path"]
                == "incident-notes.txt"
                and changed_saved["collection_id"] != original_resume_collection
                and changed_saved["session_id"] != original_resume_session
                and bench.workspace.upload_session(
                    resume_matter.matter_id, ACTOR, original_resume_session
                )[0].state
                == "cancelled"
                and bench.workspace.pending_upload_bytes(resume_matter.matter_id)
                == stale_pending_before
                - stale_session_before.total_bytes
                + sum(path.stat().st_size for path in files[:2])
                and len(
                    bench.workspace.recent_upload_sessions(
                        resume_matter.matter_id, ACTOR
                    )
                )
                == sessions_before_changed_plan + 1
                and len(
                    driver.execute_script(
                        "return window.__slice1aChangedBodies;"
                    )
                )
                == 1,
                "changed plan did not release its live stale reservation before batch-zero recovery",
            )

            javascript_errors = [
                entry
                for entry in driver.get_log("browser")
                if entry.get("level") == "SEVERE" and entry.get("source") == "javascript"
            ]
            _require(not javascript_errors, f"browser JavaScript errors: {javascript_errors}")
            report["checks"] = [
                "real matter creation and setup route",
                "folder chooser is hidden without JavaScript and revealed when enhanced",
                "metadata-only API before bytes",
                "malformed or contradictory scanner projections fail the preview closed",
                "malformed matter-capacity projections fail the preview closed",
                "response-originated display paths never reach rendered output",
                "one scanner-availability snapshot across a logical multi-batch review",
                "server-attested safe folder labels distinguish eligible and blocked rows",
                "unsafe paths and missing path attestations never reach rendered output",
                "ten-thousand-row preview uses bounded requests and one consolidated ordered result",
                "cross-batch canonical duplicate accounting",
                "matter capacity bounds the ordered ready subset across batches",
                "blocked rows cannot claim the cross-batch duplicate winner",
                "oversized single descriptor fails locally without echo or request",
                "in-flight later batch aborts without exposing partial results",
                "all five synthetic rows and exact category totals",
                "truthful pending and not-run states",
                "one capability-status check and no content scan before initial confirmation",
                "selection change invalidates and replaces prior preview",
                "failed reselection clears stale rows and totals",
                "clearing an in-flight selection clears busy state",
                "keyboard confirmation uploads only the two eligible files",
                "retained upload path reports malformed PDF as partial",
                "exact-plan cross-refresh resume and fail-closed mismatch recovery",
                "completed stale checkpoint advances through matter-scoped status recovery",
                "partial, cancelled, and missing stale checkpoints recover from batch zero",
                "status transport and projection failures preserve recovery state and start no work",
                "failed stale-session cancellation preserves recovery state and starts no work",
                "changed plan cancels its live stale reservation before fresh batch-zero recovery",
                "collection-only checkpoint cannot skip batch zero or mutate a stale collection",
                "cancelled later-batch checkpoint retries once from a fresh collection",
                "exact partial checkpoint advances without duplicating its completed batch",
                "legacy later-batch checkpoint cannot silently change its frozen plan",
                "exact v4 resume excludes completed batches from capacity accounting",
                "v4 resume mismatch preserves its exact recovery checkpoint",
                "desktop and 390px mobile views have no horizontal overflow",
                "no severe browser JavaScript error",
            ]
            report["matter_slug"] = slug
            report["selected_count"] = 5
            report["eligible_count"] = 2
            report["upload_session_state"] = session.state
            report["upload_item_states"] = [item.state for item in upload_items]
            report["scanner_status_calls"] = scanner.status_calls
            report["content_scan_calls"] = scanner.scan_calls
            report["passed"] = True
        finally:
            if driver is not None:
                driver.quit()
            if server is not None:
                server.should_exit = True
            if server_thread is not None:
                server_thread.join(timeout=10)
            if server_socket is not None:
                server_socket.close()
            if server_thread is not None and server_thread.is_alive():
                raise RuntimeError("synthetic loopback server did not stop")

    (output / "result.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
