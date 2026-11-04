"""Acceptance contract for the fictional stock case, part 1 only."""
import gc
import hashlib
import json
import re
import subprocess
import sys
import tracemalloc
from collections import defaultdict
from datetime import date, timedelta
from fractions import Fraction
from pathlib import Path

import pytest
from pypdf import PdfReader

from case_intelligence import fraud_case_gold as fraud
from case_intelligence.stock_case_gold import BROKERS, COMPANIES, build_case
from case_intelligence.stock_case_render import render


# Implementation round replaces this placeholder with the measured seed-7 digest.
SEED7_DIGEST = "PLACEHOLDER_FILL_FROM_IMPLEMENTATION"
KINDS = {
    "statement", "confirmation", "filing", "account_opening", "wire",
    "email", "text", "interview", "production",
}
DAY = r"\d{4}-\d{2}-\d{2}"
POSITION = re.compile(rf"Position: date=({DAY}); account=([\w-]+); shares=(\d+)")
TRADE = re.compile(
    rf"Trade: id=([\w-]+); date=({DAY}); account=([\w-]+); shares=([+-]?\d+)"
)
SHARES = re.compile(
    rf"Share count: effective=({DAY}); filed=({DAY}); shares=(\d+); "
    r"event=([\w_]+); split=(\d+/\d+)"
)


def rows(root, name):
    return [json.loads(line) for line in (root / name).read_text().splitlines()]


def hashes(root):
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob("*") if p.is_file()}


@pytest.fixture
def rendered(tmp_path):
    out = tmp_path / "case"
    render(out, seed=7)  # fixture-write: fresh synthetic case in temporary storage.
    return out


def test_determinism_manifest_and_cli(tmp_path):
    for name, seed in (("a", 7), ("b", 7), ("c", 8)):
        render(tmp_path / name, seed=seed)  # fixture-write: independent synthetic builds.
    # fixture-write: CLI builds synthetic documents in a fresh temporary directory.
    subprocess.run([sys.executable, "-m", "case_intelligence.stock_case_render",
                    "--out", str(tmp_path / "cli"), "--seed", "7"], check=True)
    a, b, c, cli = [hashes(tmp_path / name) for name in ("a", "b", "c", "cli")]
    assert a == b == cli
    assert a != c
    manifests = []
    for name, actual in zip(("a", "b", "c", "cli"), (a, b, c, cli)):
        manifest = json.loads((tmp_path / name / "manifest.json").read_text())
        assert manifest["sha256"] == {p: h for p, h in actual.items() if p != "manifest.json"}
        payload = "".join(f"{p}\0{h}\n" for p, h in sorted(manifest["sha256"].items()))
        assert manifest["combined_digest"] == hashlib.sha256(payload.encode()).hexdigest()
        manifests.append(manifest["combined_digest"])
    assert manifests[0] == manifests[1] == manifests[3] == SEED7_DIGEST
    assert manifests[2] != SEED7_DIGEST


def test_document_counts_and_page_locators(rendered):
    sources = rows(rendered, "sources.jsonl")
    assert 380 <= len(sources) <= 420
    assert {s["kind"] for s in sources} == KINDS
    assert len({s["doc_id"] for s in sources}) == len(sources)
    assert len({s["bates"] for s in sources}) == len(sources)
    by_doc = defaultdict(list)
    for passage in rows(rendered, "passages.jsonl"):
        by_doc[passage["doc_id"]].append(passage)
    assert set(by_doc) == {s["doc_id"] for s in sources}
    for source in sources:
        assert re.fullmatch(r"[A-Z]{3,}-\d{6}", source["bates"])
        passages = sorted(by_doc[source["doc_id"]], key=lambda p: p["page"])
        assert [p["page"] for p in passages] == list(range(1, source["pages"] + 1))
        assert all(p["text"].strip() for p in passages)
        path = rendered / source["path"]
        assert path.is_file()
        if path.suffix == ".pdf":
            pages = PdfReader(path).pages
            assert len(pages) == source["pages"]
            assert [p.extract_text().strip() for p in pages] == [p["text"].strip() for p in passages]
        if source["kind"] in {"statement", "filing"}:
            assert path.suffix == ".pdf"
            assert source["pages"] >= 2
        if source["kind"] == "statement":
            assert 3 <= source["pages"] <= 4
            assert not POSITION.search(passages[0]["text"])
            assert not any(TRADE.search(p["text"]) for p in passages[:2])
            assert any(POSITION.search(p["text"]) for p in passages[1:])


def test_daily_shape_reconstructed_from_rendered_pdfs(rendered):
    case = build_case(7)
    positions, trades, counts = {}, {}, set()
    for source in rows(rendered, "sources.jsonl"):
        if source["kind"] not in {"statement", "filing"}:
            continue
        pages = [p.extract_text() for p in PdfReader(rendered / source["path"]).pages]
        text = "\n".join(pages)
        for day, account, amount in POSITION.findall(text):
            key = (day, account)
            assert key not in positions
            positions[key] = Fraction(amount)
        for tid, day, account, amount in TRADE.findall(text):
            value = (day, account, Fraction(amount))
            assert tid not in trades or trades[tid] == value
            trades[tid] = value
        found = SHARES.findall(text)
        if source["kind"] == "filing":
            assert SHARES.search(pages[0]), "Share counts belong on filing covers"
        counts.update((effective, filed, int(n), kind, Fraction(ratio))
                      for effective, filed, n, kind, ratio in found)
    assert positions and trades and counts
    assert {row[3] for row in counts} >= {"issuance", "buyback", "split", "late_issuance"}
    assert any(effective < filed for effective, filed, _, kind, _ in counts
               if kind == "late_issuance")
    accounts = {a["account_id"] for a in case["accounts"]}
    assert len(accounts) == 5
    assert {a for _, a in positions} == accounts
    assert len({d[:7] for d, _ in positions}) == 18
    combined = set(case["combined_account_ids"])
    assert len(combined) == 4 and combined < accounts
    assert {a for _, a, _ in trades.values()} <= accounts
    activity = defaultdict(list)
    for day, account, amount in trades.values():
        activity[day].append((account, amount))
    start, end = map(date.fromisoformat, case["window"])
    assert (end.year - start.year) * 12 + end.month - start.month == 17
    assert all(start.isoformat() <= d <= end.isoformat() for d in activity)
    balances = {account: Fraction(0) for account in accounts}
    crossed, shortcut, checked = set(), set(), set()
    latest = max(counts, key=lambda row: (row[0], row[1]))[2]
    for offset in range((end - start).days + 1):
        day = (start + timedelta(days=offset)).isoformat()
        for effective, _, _, kind, ratio in sorted(counts):
            if effective == day and kind == "split":
                balances = {a: n * ratio for a, n in balances.items()}
        for account, amount in activity[day]:
            balances[account] += amount
        available = [r for r in counts if r[0] <= day and r[1] <= day]
        assert available, day
        denominator = max(available, key=lambda row: (row[0], row[1]))[2]
        assert denominator > 0
        assert all(0 <= n <= Fraction(denominator, 10) for n in balances.values()), day
        for account, amount in balances.items():
            if (day, account) in positions:
                assert amount == positions[day, account], (day, account)
                checked.add((day, account))
        total = sum(balances[a] for a in combined)
        if total > Fraction(denominator, 10):
            crossed.add(day)
        if total > Fraction(latest, 10):
            shortcut.add(day)
    assert checked == set(positions)
    first, last = map(date.fromisoformat, case["threshold_window"])
    expected = {(first + timedelta(days=i)).isoformat() for i in range((last - first).days + 1)}
    assert crossed == expected and crossed
    assert max(start, first) == first and last <= end
    buybacks = {effective for effective, _, _, kind, _ in counts if kind == "buyback"}
    assert case["buyback_date"] in buybacks
    buyback = next(r for r in counts if r[0] == case["buyback_date"] and r[3] == "buyback")
    prior = max((r for r in counts if r[0] < buyback[0]), key=lambda r: (r[0], r[1]))
    assert buyback[2] < prior[2]
    assert min(crossed) >= case["buyback_date"]
    assert len(crossed) < (end - start).days // 4
    assert crossed != shortcut


@pytest.mark.parametrize("seed", [7, 8])
def test_names_are_fictional(seed):
    case = build_case(seed)
    assert case == build_case(seed)
    assert case["matter_id"] not in {"fraud", "defense-workflow"}
    assert re.fullmatch(r"[A-Z]+-\d+", case["symbol"])
    people = {f"{a} {b}" for a in fraud.FIRST for b in fraud.LAST}
    assert case["entities"] and COMPANIES and BROKERS
    assert {e["role"] for e in case["entities"]} >= {"subject", "spouse", "associate", "issuer"}
    for entity in case["entities"]:
        allowed = people if entity["entity_type"] == "person" else set(COMPANIES) | set(BROKERS)
        assert entity["canonical_name"] in allowed
        if entity["role"] == "subject":
            assert entity["canonical_name"] != fraud.build_case(seed)["entities"][0]["canonical_name"]
    assert {a["broker_id"] for a in case["accounts"]} <= set(BROKERS)


def test_private_terms_absent_from_new_sources_and_fresh_build(rendered):
    # Mirrors the repository's private-terms check without spelling its terms here.
    substrings = "laredef rednefed gro.df sorebrek ascs oiho dnalevelc s04l 01bg tibziks notfarg newq keespeed".split()
    words = "dpf ajc".split()
    parts = [re.escape(s[::-1]) for s in substrings]
    parts += [r"\b" + re.escape(s[::-1]) + r"\b" for s in words]
    parts += [r"\b" + "lien"[::-1]]
    pattern = re.compile("|".join(parts), re.I)
    package = Path(__file__).resolve().parents[1] / "src" / "case_intelligence"
    paths = [package / f"stock_case_{suffix}.py" for suffix in ("gold", "render")]
    paths += [Path(__file__)] + [p for p in rendered.rglob("*") if p.is_file()]
    for path in paths:
        assert not pattern.search(path.name), path.name
        assert not pattern.search(path.read_bytes().decode("latin-1")), path.name


def test_streaming_memory(tmp_path):
    peaks = []
    for count in (100, 400):
        # fixture-write: warm caches with a small synthetic build before tracing.
        render(tmp_path / f"warm-{count}", seed=7, document_limit=20)
        gc.collect()
        tracemalloc.start()
        try:
            tracemalloc.reset_peak()
            out = tmp_path / f"measured-{count}"
            render(out, seed=7, document_limit=count)  # fixture-write: synthetic measured build.
            peaks.append(tracemalloc.get_traced_memory()[1])
        finally:
            tracemalloc.stop()
        assert len(rows(out, "sources.jsonl")) == count
    # Four times the documents may use 25% or 256 KB more for allocator noise.
    assert peaks[1] <= max(peaks[0] * 1.25, peaks[0] + 256_000), peaks
