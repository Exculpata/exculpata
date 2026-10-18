#!/usr/bin/env python3
"""Open an offline, disposable Harbor Street demonstration on loopback."""

from __future__ import annotations

from case_intelligence.legacy_names import (
    LEGACY_ENV_PREFIX,
    LEGACY_STEM,
)

from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
import hashlib
import json
import mimetypes
import os
from pathlib import Path
import re
import socket
import sys
import tempfile
import time
from typing import Iterator
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from fastapi import FastAPI
from fastapi.testclient import TestClient
import httpx
import uvicorn

from case_intelligence.generation import UnavailableGenerator
from case_intelligence.identity import LOGIN_CHALLENGE_COOKIE
from case_intelligence.managed_storage import StoragePolicy
from case_intelligence.store_records import MatterRecord
from case_intelligence.workbench import create_workbench_app
from synthetic_browser_environment import isolate_environment

CORPUS = ROOT / "demo_data" / "harbor-street"
PREVIEW_SUBJECT = "taylor-morgan"
PREVIEW_NAME = "Taylor Morgan"
ACTOR_ID = "development-taylor-morgan"
FLAGS = (
    "CASE_INTELLIGENCE_AUTOMATIC_DISCOVERY",
    "CASE_INTELLIGENCE_ONE_BOX",
    "CASE_INTELLIGENCE_BRIEFING",
    "CASE_INTELLIGENCE_DEEPER_INVESTIGATION",
)


@dataclass(frozen=True)
class Demo:
    app: FastAPI
    client: TestClient
    matter: MatterRecord
    actor_id: str
    receipt_url: str

    @property
    def home_path(self) -> str:
        return f"/matters/{self.matter.slug}/home"


@contextmanager
def demo_environment() -> Iterator[None]:
    """Keep the browser harness's isolation for the whole app lifetime."""
    offline = ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE")

    def relevant(name: str) -> bool:
        return name.startswith((
            "CASE_INTELLIGENCE_", "CASE_REVIEW_", f"{LEGACY_ENV_PREFIX}", "EXCULPATA_",
        )) or name in offline

    saved = {name: value for name, value in os.environ.items() if relevant(name)}
    try:
        isolate_environment()
        # The browser harness clears the legacy prefix. Application reads prefer
        # the Exculpata alias, so an inherited alias must not survive either.
        for name in tuple(os.environ):
            if name.startswith("EXCULPATA_"):
                del os.environ[name]
        os.environ.update({name: "1" for name in (*FLAGS, *offline)})
        yield
    finally:
        for name in tuple(os.environ):
            if relevant(name):
                del os.environ[name]
        os.environ.update(saved)


def require_response(response: httpx.Response, *statuses: int) -> httpx.Response:
    if response.status_code not in statuses:
        raise RuntimeError(f"Demo intake stopped: {response.request.method} "
                           f"{response.request.url.path} returned {response.status_code}.")
    return response


def intake_folder(client: TestClient, slug: str) -> str:
    """Use the same preflight, receipt and resumable intake as folder selection."""
    paths = sorted(path for path in CORPUS.rglob("*") if path.is_file())
    if not paths or any(path.is_symlink() for path in paths):
        raise ValueError("The checked-in synthetic corpus must contain regular files.")
    files = [{"name": path.name, "relative_path": path.relative_to(CORPUS).as_posix(),
              "size": path.stat().st_size,
              "media_type": mimetypes.guess_type(path.name)[0] or "application/octet-stream"}
             for path in paths]
    prefix = f"/matters/{slug}"
    preflight = require_response(client.post(prefix + "/upload-preflight", json={"files": files}), 200).json()
    indexes = list(range(len(files)))
    if preflight["eligible_indexes"] != indexes:
        raise RuntimeError("The synthetic folder did not pass intake preflight.")
    collection = "Harbor Street synthetic discovery"
    receipt = require_response(client.post(prefix + "/intake-receipts", json={
        "selection_key": uuid.uuid4().hex,
        "selection_fingerprint": hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest(),
        "selected_count": len(files), "eligible_indexes": indexes, "collection_name": collection,
    }), 201).json()
    receipt_api = prefix + "/intake-receipts/" + receipt["receipt_id"]
    require_response(client.post(receipt_api + "/items", json={"start": 0, "files": files,
        "reviewed_states": [item["state"] for item in preflight["items"]]}), 200)
    receipt = require_response(client.post(receipt_api + "/seal"), 200).json()
    session = require_response(client.post(prefix + "/upload-sessions", json={
        "files": files, "collection_name": collection,
        "intake_receipt_id": receipt["receipt_id"], "intake_ordinals": indexes,
    }), 201).json()
    for path, item in zip(paths, session["items"], strict=True):
        require_response(client.put(item["chunk_url"], content=path.read_bytes(), headers={
            "Content-Type": "application/octet-stream", "X-Upload-Offset": "0",
        }), 200)
        require_response(client.post(item["finalize_url"]), 200)
    return receipt["receipt_url"]


def discovery_has_pending_work(discovery) -> bool:
    for key in ("sources_pending", "unsealed_sources", "sources_attention", "budget_reached"):
        if discovery[key]:
            return True
    return False


def wait_for_processing(demo: Demo, expected: int, timeout_seconds: float) -> None:
    """Wait beyond extraction: durable indexing jobs and suggestions must finish."""
    bench = demo.app.state.workbench
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        readiness = bench.workspace.matter_readiness(demo.matter.matter_id)
        discovery = bench.automatic_discovery_progress(demo.matter)
        if (readiness.total_count == expected and readiness.processing_count == 0
                and readiness.searchable_count + readiness.attention_count == expected
                and readiness.can_query
                and not any(bench.workspace.active_matter_work_counts(demo.matter.matter_id).values())
                and discovery and discovery["sources_ready"] == readiness.searchable_count
                and discovery["sources_complete"] == discovery["sources_ready"]
                and not discovery_has_pending_work(discovery)):
            return
        time.sleep(0.05)
    raise TimeoutError("Harbor Street processing did not finish before the demo deadline.")


class _DemoContext:
    def __init__(self, data_dir: Path, timeout_seconds: float):
        self.data_dir = data_dir
        self.timeout_seconds = timeout_seconds
        self._stack = None

    def __enter__(self) -> Demo:
        if self._stack is not None:
            raise RuntimeError("A demo context may only be entered once.")
        with ExitStack() as stack:
            self._stack = stack
            demo = _prepare_demo(self.data_dir, self.timeout_seconds, stack)
            self._stack = stack.pop_all()
            return demo
        raise RuntimeError("Demo setup did not yield a ready demo.")

    def __exit__(self, exc_type, exc_value, traceback):
        return self._stack.__exit__(exc_type, exc_value, traceback)


def seed_demo(data_dir: Path, *, timeout_seconds: float = 120.0) -> _DemoContext:
    """Seed a fresh external directory; close the app and restore settings on exit.

    The caller owns the directory. The CLI supplies a TemporaryDirectory so no
    case data, indexes, sessions or other runtime state enter the checkout.
    Validation and resource acquisition occur when the context is entered.
    """
    return _DemoContext(data_dir, timeout_seconds)


def _prepare_demo(data_dir: Path, timeout_seconds: float, stack: ExitStack) -> Demo:
    data_dir = Path(data_dir)
    if data_dir.is_symlink() or data_dir.resolve().is_relative_to(ROOT):
        raise ValueError("Demo data must be outside the checkout in a fresh temporary directory.")
    if data_dir.exists() and (not data_dir.is_dir() or any(data_dir.iterdir())):
        raise ValueError("Demo data requires a fresh or empty directory; existing files were preserved.")
    stack.enter_context(demo_environment())
    app = create_workbench_app(data_dir.resolve(), auth_mode="preview", secure_cookie=False,
        generator=UnavailableGenerator(), learned_retrieval=False,
        background_ingestion=True, ingestion_workers=1, answer_workers=1,
        storage_policy=StoragePolicy(reserve_bytes=0), malware_scan_mode="disabled")
    # In-process HTTP uses the real session, CSRF, intake and processing
    # routes. No test authentication or direct source/index writes are used.
    client = stack.enter_context(TestClient(app, base_url="http://127.0.0.1", follow_redirects=False))
    require_response(client.get("/auth/login"), 200)
    require_response(client.post("/auth/login", data={
        "identity_subject": PREVIEW_SUBJECT,
        "login_challenge": client.cookies.get(LOGIN_CHALLENGE_COOKIE),
        "next": "/matters/new",
    }), 303)
    page = require_response(client.get("/matters/new"), 200)
    csrf = re.search(r'data-csrf-token="([0-9a-f]{64})"', page.text)
    if csrf is None:
        raise RuntimeError("Demo sign-in did not supply an intake CSRF token.")
    client.headers["X-CSRF-Token"] = csrf.group(1)
    response = require_response(client.post("/matters", data={
        "name": "Harbor Street", "descriptor": "Entirely invented records for an offline demonstration",
    }), 303)
    location = response.headers["location"]
    if not re.fullmatch(r"/matters/m-[0-9a-f]{12}/setup", location):
        raise RuntimeError("The Harbor Street demo matter could not be created.")
    matter = app.state.workbench.matter(location.split("/")[2], ACTOR_ID)
    receipt_url = intake_folder(client, matter.slug)
    demo = Demo(app, client, matter, ACTOR_ID, receipt_url)
    wait_for_processing(demo, sum(path.is_file() for path in CORPUS.rglob("*")), timeout_seconds)
    return demo


def main() -> int:
    # Check TMPDIR before creating anything: it may point into a checkout.
    parent = Path(tempfile.gettempdir()).resolve()
    if parent.is_relative_to(ROOT):
        print("Choose a system temporary directory outside the checkout with TMPDIR.", file=sys.stderr)
        return 1
    try:
        with tempfile.TemporaryDirectory(prefix=f"{LEGACY_STEM}-demo-", dir=parent) as temporary:
            print("Preparing the synthetic Harbor Street matter locally...", flush=True)
            with seed_demo(Path(temporary)) as demo, socket.socket() as listener:
                listener.bind(("127.0.0.1", 0))
                url = f"http://127.0.0.1:{listener.getsockname()[1]}{demo.home_path}"
                readiness = demo.app.state.workbench.workspace.matter_readiness(demo.matter.matter_id)
                print(f"\nHarbor Street: {readiness.searchable_count} readable, "
                      f"{readiness.attention_count} unreadable; processing finished.", flush=True)
                print(f"Open {url}", flush=True)
                print(f"Synthetic sign-in: choose {PREVIEW_NAME} (no password).", flush=True)
                print("Offline preview only. Use synthetic material. Press Ctrl-C to stop and remove demo data.", flush=True)
                # TestClient owns the single app lifespan until the server stops.
                server = uvicorn.Server(uvicorn.Config(demo.app, host="127.0.0.1",
                    lifespan="off", log_level="warning", access_log=False, proxy_headers=False))
                server.run(sockets=[listener])
    except KeyboardInterrupt:
        pass
    except (OSError, RuntimeError, ValueError, TimeoutError) as exc:
        print(f"Demo could not start: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
