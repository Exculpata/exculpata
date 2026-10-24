#!/usr/bin/env python3
"""Record before/after HTTP evidence from a disposable synthetic workbench."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
QUERY = "Vela Quenrix"
PHASES = ("before", "after")


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", choices=PHASES, required=True)
    parser.add_argument("--evidence", type=Path, required=True, help="JSON file outside the checkout")
    parser.add_argument("--checkout", type=Path, default=ROOT,
                        help="Tree to seed and serve from; default: this script's checkout")
    parser.add_argument("--allow-same-revision", action="store_true",
                        help="Allow the after revision to equal before; recorded in evidence")
    parser.add_argument("--timeout", type=float, default=120.0,
                        help="Seconds allowed for seeding and server startup (each); default: 120")
    args = parser.parse_args(argv)
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error("--timeout must be finite and greater than zero")
    args.checkout = args.checkout.resolve()
    return args


def read_evidence(path):
    if not path.exists():
        return {"schema_version": 1, "synthetic_only": True, "runs": {}}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if (not isinstance(payload, dict) or payload.get("schema_version") != 1
            or payload.get("synthetic_only") is not True
            or not isinstance(payload.get("runs"), dict)
            or any(key not in PHASES or not isinstance(value, dict)
                   for key, value in payload["runs"].items())):
        raise ValueError("Unrecognized verification evidence; choose a new JSON file")
    return payload


def write_evidence(path, phase, report):
    """Preserve the other phase and replace the file only after serialization."""
    if phase not in PHASES:
        raise ValueError("Unknown verification phase")
    payload = read_evidence(path)
    if phase in payload["runs"]:
        raise ValueError(f"{phase} already recorded; choose a new evidence file")
    payload["runs"][phase] = report
    encoded = json.dumps(payload, indent=2, sort_keys=True, allow_nan=False) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=".verify-exculpata-", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(encoded)
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


@contextmanager
def serve_loopback(app, timeout):
    import uvicorn

    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        base = f"http://127.0.0.1:{listener.getsockname()[1]}"
        # seed_demo owns the app lifespan, including ingestion workers.
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", lifespan="off",
            log_level="warning", access_log=False, proxy_headers=False))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + timeout
            while not server.started:
                if not thread.is_alive() or time.monotonic() >= deadline:
                    raise TimeoutError("Loopback workbench did not start")
                time.sleep(0.02)
            yield base
        finally:
            server.should_exit = True
            thread.join(timeout=10)
            if thread.is_alive():
                raise TimeoutError("Loopback workbench did not stop")


def observe(client, report, step, path, **kwargs):
    row = {"step": step, "status": None, "fields": {}, "passed": False}
    report["checks"].append(row)
    started = time.monotonic()
    try:
        response = client.get(path, **kwargs)
        row["status"] = response.status_code
        row["fields"]["content_type"] = response.headers.get("content-type", "")
    finally:
        row["elapsed_ms"] = round((time.monotonic() - started) * 1000, 3)
    require(response.status_code == 200, step)
    return response, row


def require(condition, step):
    if not condition:
        raise RuntimeError(f"Verification failed at {step}")


def drive_flows(client, demo, report, expected):
    prefix = f"/matters/{demo.matter.slug}"
    response, row = observe(client, report, "load_case", demo.home_path)
    row["fields"]["synthetic_case_visible"] = "Harbor Street" in response.text
    require(row["fields"]["synthetic_case_visible"], "load_case")
    row["passed"] = True

    response, row = observe(client, report, "readiness", prefix + "/processing-status")
    readiness = response.json()
    keys = ("total_count", "saved_count", "searchable_count", "attention_count",
            "processing_count", "can_query", "partial_query")
    row["fields"].update({key: readiness.get(key) for key in keys})
    require(readiness["total_count"] == readiness["saved_count"] == expected
            and readiness["searchable_count"] == expected - 1
            and readiness["attention_count"] == 1 and readiness["processing_count"] == 0
            and readiness["can_query"] and not readiness["active_work"], "readiness")
    row["passed"] = True

    response, row = observe(client, report, "search", prefix + "/exact-search",
                            params={"words": QUERY, "search": "1"})
    count = re.search(r'id="find-results-heading">([\d,]+) sources? found', response.text)
    row["fields"].update(query=QUERY, sources_found=int(count[1].replace(",", "")) if count else 0)
    # Only take a relative source path within this matter; never follow a URL or redirect.
    source = re.search(r'class="find-open-source" href="('
                       + re.escape(prefix) + r'/sources/[A-Za-z0-9_-]+)(?:[?"])', response.text)
    require(row["fields"]["sources_found"] >= 2 and source is not None, "search")
    row["passed"] = True

    response, row = observe(client, report, "open_document", source[1])
    extracted = re.search(r"<pre>(.*?)</pre>", response.text, re.DOTALL)
    row["fields"].update(review_workspace="data-review-workspace" in response.text,
                          searchable_section="Selected searchable section" in response.text,
                          matching_text_visible=bool(extracted and QUERY in extracted[1]))
    require(all(row["fields"].get(key) for key in
                ("review_workspace", "searchable_section", "matching_text_visible")),
            "open_document")
    row["passed"] = True


def run_verification(args, report):
    # Keep evidence/argument helpers usable without importing or starting the app.
    if not all((args.checkout / path).is_file() for path in
               ("scripts/demo.py", "src/case_intelligence/__init__.py")):
        raise ValueError("--checkout must contain scripts/demo.py and src/case_intelligence")
    sys.path[:0] = [str(args.checkout / "src"), str(args.checkout / "scripts")]
    import httpx
    from demo import CORPUS, seed_demo

    parent = Path(tempfile.gettempdir()).resolve()
    if any(parent.is_relative_to(tree) for tree in (ROOT, args.checkout)):
        raise ValueError("TMPDIR must be outside the checkout")
    with tempfile.TemporaryDirectory(prefix="exculpata-verify-", dir=parent) as temporary:
        started = time.monotonic()
        with seed_demo(Path(temporary), timeout_seconds=args.timeout) as demo:
            report["seed_ms"] = round((time.monotonic() - started) * 1000, 3)
            with serve_loopback(demo.app, args.timeout) as base:
                # Inherit only the synthetic session; environment proxies and redirects are disabled.
                with httpx.Client(base_url=base, cookies=demo.client.cookies,
                                  trust_env=False, follow_redirects=False, timeout=30) as client:
                    drive_flows(client, demo, report,
                                sum(path.is_file() for path in CORPUS.rglob("*")))


def main(argv=None):
    args = parse_args(argv)
    try:
        if any(args.evidence.resolve().is_relative_to(tree) for tree in (ROOT, args.checkout)):
            raise ValueError("Evidence must be outside the checkout")
        runs = read_evidence(args.evidence)["runs"]
        if args.phase in runs:
            raise ValueError(f"{args.phase} already recorded; choose a new evidence file")
        revision = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=args.checkout, text=True).strip()
        if args.phase == "after":
            before_revision = runs.get("before", {}).get("revision")
            if not isinstance(before_revision, str) or not before_revision:
                raise ValueError("Record a before phase with a revision first")
            if revision == before_revision and not args.allow_same_revision:
                raise ValueError("After revision equals before; use --allow-same-revision for a smoke check")
        dirty = bool(subprocess.check_output(
            ["git", "status", "--porcelain"], cwd=args.checkout, text=True).strip())
        args.evidence.parent.mkdir(parents=True, exist_ok=True)
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    report = {"started_at": datetime.now(timezone.utc).isoformat(),
              "revision": revision, "working_tree_dirty": dirty,
              "allow_same_revision": args.allow_same_revision, "passed": False, "checks": []}
    started = time.monotonic()
    try:
        run_verification(args, report)
        report["passed"] = True
    except (Exception, KeyboardInterrupt) as exc:
        # Record the failure class and partial checks, never environment or response dumps.
        report["error_type"] = type(exc).__name__
    report["total_ms"] = round((time.monotonic() - started) * 1000, 3)
    try:
        write_evidence(args.evidence, args.phase, report)
    except (OSError, ValueError) as exc:
        print(f"Evidence could not be written: {exc}", file=sys.stderr)
        return 1
    print(f"{args.phase}: {'PASS' if report['passed'] else 'FAIL'}; evidence: {args.evidence}")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
