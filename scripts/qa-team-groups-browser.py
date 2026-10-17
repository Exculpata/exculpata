#!/usr/bin/env python3
"""Run synthetic Team groups browser acceptance in an isolated loopback HTTPS node.

Requires this checkout's Python dependencies, Node, OpenSSL and Playwright with
Chromium. Set PLAYWRIGHT_MODULE to an installed playwright package when it is
not resolvable from the checkout. No existing node is contacted or modified.
"""

from __future__ import annotations

from case_intelligence.legacy_names import (
    LEGACY_ENV_PREFIX,
    LEGACY_STEM,
    name_value,
)

import argparse
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def _isolate_environment() -> None:
    """Discard inherited deployment configuration before importing the runtime."""
    for name in tuple(os.environ):
        if name.startswith(("CASE_INTELLIGENCE_", "CASE_REVIEW_")) or (
                name.startswith(f"{LEGACY_ENV_PREFIX}") and not name.startswith(f"{LEGACY_ENV_PREFIX}QA_")):
            del os.environ[name]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifacts", type=Path, help="optional screenshot directory outside the checkout")
    args = parser.parse_args()
    for command in ("node", "openssl"):
        if shutil.which(command) is None:
            parser.error(f"{command} is required for browser acceptance")
    if args.artifacts is not None and args.artifacts.resolve().is_relative_to(ROOT):
        parser.error("keep generated screenshots outside the source checkout")
    _isolate_environment()
    from case_intelligence.generation import UnavailableGenerator
    from case_intelligence.identity import LocalAccountSettings
    from case_intelligence.local_accounts import LocalAccountRepository
    from case_intelligence.workbench import create_workbench_app
    import uvicorn

    with tempfile.TemporaryDirectory(prefix=f"{LEGACY_STEM}-groups-acceptance-") as directory:
        root = Path(directory).resolve()
        cert, key = root / "loopback.crt", root / "loopback.key"
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                        "-subj", "/CN=localhost", "-keyout", str(key), "-out", str(cert)],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        repository = LocalAccountRepository(root / "accounts/local-accounts.json")
        repository.initialize("alice.admin", "Alice Administrator", "synthetic-browser-password", actor="synthetic-operator")
        os.environ["CASE_INTELLIGENCE_STORAGE_RESERVE_GIB"] = "0"
        os.environ["CASE_INTELLIGENCE_TEST_MODE"] = "1"
        app = create_workbench_app(root / "runtime", generator=UnavailableGenerator(), auth_mode="local",
            secure_cookie=True, local_settings=LocalAccountSettings(repository.path, management_root=repository.path.parent),
            learned_retrieval=False, answer_workers=1)
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(64)
        server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False,
            ssl_keyfile=str(key), ssl_certfile=str(cert)))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 20
            while not server.started and thread.is_alive() and time.monotonic() < deadline:
                time.sleep(0.05)
            if not server.started:
                raise RuntimeError("Synthetic acceptance node did not start")
            artifacts = args.artifacts.resolve() if args.artifacts else root / "screenshots"
            artifacts.mkdir(parents=True, exist_ok=True)
            env = os.environ.copy()
            env.update({
                name_value("environment", suffix="QA_ORIGIN"): f"https://127.0.0.1:{listener.getsockname()[1]}",
                name_value("environment", suffix="QA_ARTIFACTS"): str(artifacts),
            })
            result = subprocess.run(["node", str(ROOT / "scripts/qa-team-groups-browser.cjs")], env=env, cwd=ROOT)
            return result.returncode
        finally:
            server.should_exit = True
            thread.join(timeout=15)
            listener.close()


if __name__ == "__main__":
    raise SystemExit(main())
