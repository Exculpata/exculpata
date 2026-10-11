#!/usr/bin/env python3
"""Measure synthetic folder intake with the real app; write only outside the checkout."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import cProfile
import hashlib
import json
import os
from pathlib import Path
import platform
import pstats
import random
import resource
import subprocess
import sys
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
TYPES = {"txt": "text/plain", "eml": "message/rfc822", "csv": "text/csv", "pdf": "application/pdf"}
STAGES = ("selection", "intake", "extraction", "indexing", "automatic_discovery", "can_query", "build_briefing")
POLL_SECONDS = 0.05


def outside_checkout(value: str | Path) -> Path:
    path = Path(value).expanduser().resolve()
    if path.is_relative_to(ROOT):
        raise ValueError("Benchmark artifacts must be outside the checkout.")
    return path


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def pdf_bytes(lines: list[str]) -> bytes:
    """A native-text, one-page PDF with no timestamps, identity or external assets."""
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
    from io import BytesIO

    writer = PdfWriter()
    page = writer.add_blank_page(width=612, height=792)
    font = DictionaryObject({NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/Type1"), NameObject("/BaseFont"): NameObject("/Helvetica")})
    page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"):
        DictionaryObject({NameObject("/F1"): writer._add_object(font)})})
    stream = DecodedStreamObject()
    escaped = [line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)") for line in lines]
    stream.set_data(("BT /F1 10 Tf 45 745 Td 14 TL " +
        " ".join(f"({line}) Tj T*" for line in escaped) + " ET").encode("ascii"))
    page[NameObject("/Contents")] = writer._add_object(stream)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


def generate_corpus(directory: Path, count: int, seed: int, density: str) -> dict:
    rng = random.Random(seed)
    digest = hashlib.sha256()
    counts = dict.fromkeys(TYPES, 0)
    total = 0
    for index in range(count):
        extension = tuple(TYPES)[index % len(TYPES)]
        custodian = ("Alex Example", "Jordan Sample", "Riley Placeholder")[index % 3]
        day = 1 + rng.randrange(28)
        folder = directory / f"production-{index // 500 + 1:03d}" / custodian.replace(" ", "-") / {
            "txt": "notes", "eml": "correspondence", "csv": "schedules", "pdf": "reports"}[extension]
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"record-{index + 1:06d}.{extension}"
        lines = [f"Synthetic record {index + 1:06d}; invented benchmark material only.",
            "The delivery schedule was reviewed at the sample depot."]
        # A bounded entity-bearing subset exercises discovery without exhausting
        # its existing storage budget: repeated names are separate suggestions.
        if index % 100 == 0:
            lines.append(f"{custodian} met Morgan Example on 04/{day:02d}/2026.")
        lines += [f"Entry {number + 1}: sample crate {rng.randrange(100, 999)} was checked; no damage noted."
                  for number in range(8 + rng.randrange(8))]
        if density == "dense":
            lines = [lines[0]] + [
                f"Alex Example met Jordan Sample on 04/{day:02d}/2026; entry {number + 1}."
                for number in range(20)]
        if extension == "pdf":
            content = pdf_bytes(lines)
        elif extension == "eml":
            content = ("From: alex@example.test\nTo: jordan@example.test\n"
                f"Date: {day:02d} Apr 2026 10:00:00 +0000\nSubject: Synthetic delivery {index + 1}\n"
                "MIME-Version: 1.0\nContent-Type: text/plain; charset=utf-8\n\n" + "\n".join(lines)).encode()
        elif extension == "csv":
            content = ("record,batch,reviewer,note\n" + "\n".join(
                f'{index + 1}-{number},batch-{day:02d},reviewer-{index % 3 + 1},"{line}"'
                for number, line in enumerate(lines))).encode()
        else:
            content = ("\n".join(lines) + "\n").encode()
        path.write_bytes(content)
        digest.update(path.relative_to(directory).as_posix().encode() + b"\0" + content + b"\0")
        total += len(content)
        counts[extension] += 1
    return {"sha256": digest.hexdigest(), "bytes": total, "type_counts": counts}


@contextmanager
def profile_all_threads(enabled: bool):
    """Python 3.12+ cProfile monitors the interpreter, including app threads."""
    profiler = cProfile.Profile()
    if enabled:
        profiler.enable()
    try:
        yield [profiler] if enabled else []
    finally:
        if enabled:
            profiler.disable()


def profile_summary(profiles: list) -> dict | None:
    if not profiles:
        return None
    stats = pstats.Stats(profiles[0])
    for profiler in profiles[1:]:
        stats.add(profiler)
    rows = []
    for (filename, line, function), (primitive, calls, own, cumulative, _callers) in stats.stats.items():
        # Rank application functions by self time: idle locks/selectors and inclusive
        # thread runners would otherwise obscure the actionable work.
        path = Path(filename)
        if not path.is_relative_to(ROOT / "src" / "case_intelligence"):
            continue
        rows.append({"file": path.relative_to(ROOT).as_posix(), "line": line,
            "function": function, "calls": calls, "self_seconds": own, "cumulative_seconds": cumulative})
    return {"scope": "all_threads_application_functions_by_self_time",
        "top_three": sorted(rows, key=lambda row: row["self_seconds"], reverse=True)[:3]}


def isolate_environment() -> None:
    for key in tuple(os.environ):
        if key.startswith(("CASE_INTELLIGENCE_", "RECORDBENCH_", "PG")):
            del os.environ[key]
    os.environ.update(CASE_INTELLIGENCE_AUTOMATIC_DISCOVERY="1",
        CASE_INTELLIGENCE_STORAGE_RESERVE_GIB="0")

    def deny_network(event, args):
        if event in {"socket.connect", "socket.getaddrinfo"}:
            raise RuntimeError("Network access is disabled in the synthetic benchmark.")
    sys.addaudithook(deny_network)


class ReadinessObserver:
    """Integrate sampled active intervals, plus first observed whole-corpus milestones."""
    def __init__(self, bench, matter, count: int, started: float):
        self.bench, self.matter, self.count, self.started = bench, matter, count, started
        self.stages = {key: {"status": "pending", "wall_seconds": 0.0,
            "completed_seconds": None} for key in STAGES}
        self.stages["build_briefing"] = {"status": "not_available", "wall_seconds": None,
            "completed_seconds": None}
        self.previous = started
        self.active = dict.fromkeys(("extraction", "indexing", "automatic_discovery"), False)
        self.readiness = None
        self.discovery = None
        self.budget_exhausted_seconds = None
        self.lock = threading.Lock()

    def sample(self) -> bool:
        with self.lock:
            record = self.bench.workspace.matter_readiness(self.matter.matter_id)
            progress = self.bench.automatic_discovery_progress(self.matter)
            now = time.perf_counter()
            elapsed = now - self.started
            for key, active in self.active.items():
                if active:
                    self.stages[key]["wall_seconds"] += now - self.previous
            self.previous = now
            self.active = {"extraction": record.extracting_count > 0,
                "indexing": record.indexing_count > 0,
                "automatic_discovery": bool(progress and
                    not progress["budget_reached"] and
                    progress["sources_complete"] < progress["sources_ready"])}
            if progress["budget_reached"] and self.budget_exhausted_seconds is None:
                self.budget_exhausted_seconds = elapsed
                self.stages["automatic_discovery"]["status"] = "budget_exhausted"
            complete = {"intake": record.saved_count == self.count,
                "extraction": record.extracted_count == self.count,
                "indexing": record.searchable_count == self.count,
                "can_query": record.can_query and record.searchable_count == self.count,
                "automatic_discovery": bool(progress and progress["sources_complete"] == self.count)}
            for key, done in complete.items():
                if record.total_count == self.count and done and self.stages[key]["status"] == "pending":
                    self.stages[key].update(status="complete", completed_seconds=elapsed)
                    if key == "can_query":
                        self.stages[key]["wall_seconds"] = elapsed
            self.readiness = {key: getattr(record, key) for key in ("total_count", "saved_count",
                "extracted_count", "searchable_count", "attention_count", "can_query")}
            self.discovery = {key: progress[key] for key in ("sources_ready", "sources_complete",
                "sources_attention", "sources_pending", "unsealed_sources", "budget_reached")}
            if record.attention_count or progress["sources_attention"]:
                raise RuntimeError("Synthetic intake or discovery did not complete successfully.")
            # A budget pause can be temporary while later uploads are still
            # unsealed. Wait for the final whole-corpus pause, not an early batch.
            discovery_terminal = progress["sources_complete"] == self.count or progress["budget_reached"]
            return discovery_terminal and all(
                self.stages[key]["status"] in {"complete", "budget_exhausted"} for key in complete)


def request(client, method: str, url: str, **kwargs):
    response = client.request(method, url, **kwargs)
    if response.status_code not in (200, 201, 303):
        raise RuntimeError(f"Synthetic intake request failed (HTTP {response.status_code}).")
    return response


def run_one(count: int, seed: int, density: str, temp_root: Path, timeout: float, briefing: bool) -> dict:
    from fastapi.testclient import TestClient
    from case_intelligence.generation import UnavailableGenerator
    from case_intelligence.source_locations import SourceLocationRegistry
    from case_intelligence.workbench import create_workbench_app

    corpus_dir = temp_root / "corpus"
    corpus = generate_corpus(corpus_dir, count, seed, density)
    runtime = temp_root / "runtime"
    app = create_workbench_app(runtime, auth_mode="test", generator=UnavailableGenerator(),
        source_registry=SourceLocationRegistry(), background_ingestion=True, ingestion_workers=2,
        learned_retrieval=False, malware_scan_mode="disabled")
    with TestClient(app) as client:
        response = request(client, "POST", "/matters", data={"name": "Synthetic benchmark",
            "descriptor": "Invented discovery only"}, follow_redirects=False)
        slug = response.headers["location"].split("/")[2]
        base = f"/matters/{slug}"
        bench = app.state.workbench
        actor = "development-taylor-morgan"
        matter = bench.matter(slug, actor)
        started = time.perf_counter()
        paths = sorted(corpus_dir.rglob("*"))
        paths = [path for path in paths if path.is_file()]
        descriptors = [{"name": path.name, "relative_path": path.relative_to(corpus_dir).as_posix(),
            "size": path.stat().st_size, "media_type": TYPES[path.suffix[1:]]} for path in paths]
        preflight = request(client, "POST", base + "/upload-preflight",
            json={"files": descriptors, "selection_nonce": "a" * 32}).json()
        if preflight["eligible_indexes"] != list(range(count)):
            raise RuntimeError("The synthetic selection was not fully eligible.")
        receipt = request(client, "POST", base + "/intake-receipts", json={
            "selection_key": "a" * 32, "selection_fingerprint": corpus["sha256"], "selected_count": count,
            "eligible_indexes": preflight["eligible_indexes"], "collection_name": "Synthetic production"}).json()
        receipt_url = base + "/intake-receipts/" + receipt["receipt_id"]
        batch_size = 2000
        for offset in range(0, count, batch_size):
            batch = descriptors[offset:offset + batch_size]
            request(client, "POST", receipt_url + "/items", json={"start": offset, "files": batch,
                "reviewed_states": ["valid"] * len(batch)})
        request(client, "POST", receipt_url + "/seal")
        observer = ReadinessObserver(bench, matter, count, started)
        selected = time.perf_counter() - started
        observer.stages["selection"].update(status="complete", wall_seconds=selected, completed_seconds=selected)
        stopped = threading.Event()
        errors = []

        def observe():
            try:
                while not stopped.wait(POLL_SECONDS):
                    observer.sample()
            except Exception as exc:
                errors.append(exc)
                stopped.set()

        sampler = threading.Thread(target=observe, name="benchmark-readiness")
        sampler.start()
        intake_started = time.perf_counter()
        try:
            # Admit all sessions first: readiness cannot briefly mean only an early subset.
            sessions = []
            collection_id = None
            for offset in range(0, count, batch_size):
                batch = descriptors[offset:offset + batch_size]
                payload = {"files": batch, "intake_receipt_id": receipt["receipt_id"],
                    "intake_ordinals": list(range(offset, offset + len(batch))),
                    "collection_name": "Synthetic production"}
                if collection_id:
                    payload["collection_id"] = collection_id
                session = request(client, "POST", base + "/upload-sessions", json=payload).json()
                collection_id = session["collection_id"]
                sessions.extend(session["items"])
            for path, item in zip(paths, sessions, strict=True):
                if errors:
                    raise errors[0]
                if time.perf_counter() - started > timeout:
                    raise TimeoutError("Synthetic benchmark exceeded its time limit.")
                request(client, "PUT", item["chunk_url"], content=path.read_bytes(),
                    headers={"Content-Type": "application/octet-stream", "X-Upload-Offset": "0"})
                request(client, "POST", item["finalize_url"])
            while not observer.sample():
                if errors:
                    raise errors[0]
                if time.perf_counter() - started > timeout:
                    raise TimeoutError("Synthetic benchmark exceeded its time limit.")
                time.sleep(POLL_SECONDS)
        finally:
            stopped.set()
            sampler.join()
        if errors:
            raise errors[0]
        observer.stages["intake"]["wall_seconds"] = (
            observer.stages["intake"]["completed_seconds"] - (intake_started - started))
        if briefing:
            from case_intelligence.briefing import build_briefing
            from case_intelligence.intake_receipts import IntakeReceipts

            briefing_started = time.perf_counter()
            build_briefing(matter, actor, workspace=bench.workspace,
                entity_service=bench.entity_service(matter), intake_receipts=IntakeReceipts(bench.workspace),
                source_metadata=bench.source_store(matter).get)
            observer.stages["build_briefing"].update(status="complete",
                wall_seconds=time.perf_counter() - briefing_started,
                completed_seconds=time.perf_counter() - started)
    database_bytes = sum(path.stat().st_size for path in runtime.rglob("*")
        if path.is_file() and path.suffix in {".sqlite", ".sqlite3", ".db"})
    return {"files": count, "corpus": corpus, "stages": observer.stages,
        "outcome": "discovery_budget_exhausted" if observer.budget_exhausted_seconds is not None else "complete",
        "budget_exhausted_seconds": observer.budget_exhausted_seconds,
        "readiness": observer.readiness, "automatic_discovery": observer.discovery,
        "peak_rss_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * (1 if sys.platform == "darwin" else 1024),
        "database_bytes": database_bytes}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sizes", type=int, nargs="+", default=[100, 1000, 5000])
    parser.add_argument("--seed", type=int, default=20261009)
    parser.add_argument("--density", choices=("sparse", "dense"), default="sparse",
        help="Sparse references for scale timings, or repeated people/dates to exercise the discovery budget")
    parser.add_argument("--output", required=True, type=outside_checkout)
    parser.add_argument("--temp-root", type=outside_checkout, default=Path("/tmp"))
    parser.add_argument("--timeout", type=float, default=7200, help="Per-size time limit in seconds")
    parser.add_argument("--profile", action="store_true", help="Profile all worker threads; changes timings")
    parser.add_argument("--main-ref", default="origin/main",
        help="Main revision to inspect; shape tests in shallow checkouts can explicitly use HEAD")
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--briefing", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if any(size < 1 or size > 10000 for size in args.sizes) or not 0 < args.timeout < float("inf"):
        parser.error("Use sizes from 1 to 10000 and a finite positive timeout.")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.temp_root.mkdir(parents=True, exist_ok=True)
    if args.worker:
        isolate_environment()
        with tempfile.TemporaryDirectory(prefix="recordbench-timing-", dir=args.temp_root) as directory:
            with profile_all_threads(args.profile) as profiles:
                run = run_one(args.sizes[0], args.seed, args.density, Path(directory), args.timeout, args.briefing)
            run["profile"] = profile_summary(profiles)
            write_json(args.output, run)
        return

    def git(*arguments):
        return subprocess.check_output(["git", "-C", str(ROOT), *arguments], text=True).strip()

    main_commit = git("rev-parse", args.main_ref)
    briefing = bool(git("ls-tree", main_commit, "src/case_intelligence/briefing.py"))
    if briefing and not (ROOT / "src/case_intelligence/briefing.py").is_file():
        parser.error("Update this branch from main before timing its available briefing module.")
    result = {"schema_version": 1,
        "git": {"commit": git("rev-parse", "HEAD"), "clean": not bool(git("status", "--porcelain")),
            "main_commit": main_commit, "briefing_available_on_main": briefing},
        "environment": {"python": platform.python_version(), "cpu_count": os.cpu_count()},
        "configuration": {"seed": args.seed, "poll_seconds": POLL_SECONDS, "ingestion_workers": 2,
            "profiled": args.profile, "runtime": "sqlite_basic_offline", "density": args.density}, "runs": []}
    for count in args.sizes:
        with tempfile.TemporaryDirectory(prefix="recordbench-receipt-", dir=args.temp_root) as directory:
            output = Path(directory) / "run.json"
            command = [sys.executable, str(Path(__file__).resolve()), "--worker", "--sizes", str(count),
                "--seed", str(args.seed), "--output", str(output), "--temp-root", str(Path(directory) / "work"),
                "--timeout", str(args.timeout), "--density", args.density]
            if args.profile:
                command.append("--profile")
            if briefing:
                command.append("--briefing")
            subprocess.run(command, check=True, timeout=args.timeout + 120, cwd=ROOT)
            result["runs"].append(json.loads(output.read_text(encoding="utf-8")))
            write_json(args.output, result)
            print(f"Completed {count} synthetic files.", file=sys.stderr)


if __name__ == "__main__":
    main()
