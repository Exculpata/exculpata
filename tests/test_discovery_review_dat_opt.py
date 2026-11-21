"""Generated DAT/OPT preservation, association and real review-surface contracts."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from types import SimpleNamespace
import hashlib
import html
import json
from pathlib import Path
import re
import threading
import time
from urllib.parse import parse_qs, urlsplit
import uuid

import pytest
from fastapi.testclient import TestClient

from case_intelligence.generation import UnavailableGenerator
from case_intelligence.managed_storage import StoragePolicy
from case_intelligence.modules import ModuleContext, ModuleDatabase
from case_intelligence.modules.discovery_review import fields, load_files
from case_intelligence.modules.discovery_review.identifiers import IdentifierConflict, IdentifierRepository
from case_intelligence.modules.discovery_review.production import (
    ProductionRepository, ProductionService, SourceIdentity, definition_digest,
)
from case_intelligence.source_load_files import RecordEvent
from case_intelligence.workbench import create_workbench_app
from tests.fixture_write import fixture_write

ACTOR = "development-taylor-morgan"
MAX_ORDINAL = 9_223_372_036_854_775_807


@pytest.fixture(autouse=True)
def zero_storage_reserve(monkeypatch):
    monkeypatch.setenv("CASE_INTELLIGENCE_STORAGE_RESERVE_GIB", "0")


def dat_bytes(headers, rows, *, encoding="utf-8", newline="\r\n"):
    def record(values):
        return "\x14".join("þ" + value + "þ" for value in values) + newline
    text = record(headers) + "".join(record(row) for row in rows)
    if encoding == "utf-16-le":
        return b"\xff\xfe" + text.encode(encoding)
    if encoding == "utf-16-be":
        return b"\xfe\xff" + text.encode(encoding)
    return text.encode(encoding)


def opt_bytes(rows):
    return "".join(",".join(row) + "\r\n" for row in rows).encode()


def source_text(number):
    return f"Generated source {number} carries searchable copper text.\n".encode()


def review_for(client, matter):
    bench = client.app.state.workbench
    database = ModuleDatabase(bench.workspace.connection, bench.workspace._lock)
    def capacity(size):
        with bench._storage_reservation_lock:
            bench.storage.ensure_capacity(bench.workspace.pending_upload_bytes()
                + bench._playback_reserved_bytes() + size)
    context = ModuleContext(database, bench.workspace.membership, bench.source_store,
        bench.storage.matters, bench.malware_scanner, bench.malware_scan_mode, capacity)
    repository = ProductionRepository(database)
    service = ProductionService(context, repository=repository)
    definition = fields.load_definition()
    service.bind(matter, bench.source_store(matter), definition)
    review = SimpleNamespace(client=client, bench=bench, matter=matter, repository=repository,
        service=service, definition=definition, store=bench.source_store(matter),
        base=f"/matters/{matter.slug}/production")

    def document(path):
        return next(value for value in review.store.documents.values() if value.relative_path == path)

    def token(path):
        return review.store.action_token(document(path))

    def identity(path):
        value = document(path)
        root = review.repository.provenance(matter.matter_id, value.document_id, value.version_id)
        return SourceIdentity(value.document_id, value.version_id, value.digest,
                             value.display_name, value.relative_path, Path(path).suffix[1:].casefold(), root)

    def projection(path):
        value = document(path)
        return review.service.document_projection(matter.matter_id, value.document_id, value.version_id)

    review.document, review.token, review.identity, review.projection = document, token, identity, projection
    return review


@contextmanager
def opened_review(runtime, slug=None):
    app = create_workbench_app(runtime, generator=UnavailableGenerator(), auth_mode="test",
        background_ingestion=True, ingestion_workers=1, malware_scan_mode="disabled",
        storage_policy=StoragePolicy(reserve_bytes=0))
    with TestClient(app) as client:
        if slug is None:
            response = client.post("/matters", data={"name": "Generated production review"},
                                   follow_redirects=False)
            assert response.status_code == 303, response.text
            slug = response.headers["location"].split("/")[2]
        matter = app.state.workbench.matter(slug, ACTOR)
        yield review_for(client, matter)


@pytest.fixture
def review(tmp_path, monkeypatch):
    monkeypatch.setenv("CASE_INTELLIGENCE_STORAGE_RESERVE_GIB", "0")
    with opened_review(tmp_path / "runtime") as current:
        yield current


def upload_production(review, files, *, eligible=None, receipt=None):
    """Use one actual receipt; a shared directory alone never supplies provenance."""
    names = list(files)
    described = [{"name": name.rsplit("/", 1)[-1], "relative_path": name,
                  "size": len(files[name]), "media_type": "text/plain"} for name in names]
    indexes = list(range(len(names))) if eligible is None else list(eligible)
    root = f"/matters/{review.matter.slug}/intake-receipts"
    if receipt is None:
        response = review.client.post(root, json={"selection_key": uuid.uuid4().hex,
            "selection_fingerprint": hashlib.sha256(repr(names).encode()).hexdigest(),
            "selected_count": len(names), "eligible_indexes": list(range(len(names))),
            "collection_name": "Generated production intake"})
        assert response.status_code == 201, response.text
        receipt = response.json()
        receipt_root = root + "/" + receipt["receipt_id"]
        response = review.client.post(receipt_root + "/items", json={"start": 0,
            "files": described, "reviewed_states": ["valid"] * len(names)})
        assert response.status_code == 200, response.text
        assert review.client.post(receipt_root + "/seal").status_code == 200
    response = review.client.post(f"/matters/{review.matter.slug}/upload-sessions", json={
        "files": [described[index] for index in indexes],
        "collection_name": "Generated production intake", "intake_receipt_id": receipt["receipt_id"],
        "intake_ordinals": indexes})
    assert response.status_code in {200, 201}, response.text
    session = response.json()
    for item, ordinal in zip(session["items"], indexes):
        body = files[names[ordinal]]
        for offset in range(0, len(body), 1024 * 1024):
            response = review.client.put(item["chunk_url"], content=body[offset:offset + 1024 * 1024],
                headers={"Content-Type": "application/octet-stream", "X-Upload-Offset": str(offset)})
            assert response.status_code == 200, response.text
        response = review.client.post(item["finalize_url"])
        assert response.status_code == 200, response.text
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        items = [review.bench.workspace.upload_item(review.matter.matter_id, ACTOR,
            session["upload_session_id"], item["upload_item_id"]) for item in session["items"]]
        jobs = [review.bench.workspace.ingest_job(review.matter.matter_id, item.document_id) for item in items]
        if all(job is not None and job.state in {"succeeded", "failed"} for job in jobs):
            assert all(job.state == "succeeded" for job in jobs), jobs
            return receipt
        time.sleep(0.01)
    pytest.fail("Generated production intake did not finish real ingestion")


def import_paths(review, paths, *, expected_failed=()):
    attempts = {}
    review.service.bind(review.matter, review.store, review.definition)
    target = f"{review.store.revision}:{definition_digest(review.definition)}"
    for path in paths:
        source = review.identity(path)
        assert source.root_key, "The real intake receipt must establish the production root"
        table = None
        if source.source_format in {"csv", "tsv"}:
            table = load_files.parse(review.store.source_path(source.document_id).read_bytes(),
                document_id=source.document_id, name=path, definition=review.definition)
            assert bool(table.problem) is (path in expected_failed)
        while True:
            if table is not None:
                attempt = review.service.import_legacy_table(review.matter.matter_id, source,
                    table, review.definition, target=target)
            else:
                attempt = review.service.import_source(review.matter.matter_id, source,
                    review.store.source_path(source.document_id, verify_digest=True), review.definition,
                    target=target, max_records=256, max_bytes=4 * 1024 * 1024)
            assert attempt["status"] != "stale", attempt
            if attempt["status"] == "failed":
                assert path in expected_failed and attempt["phase"] == "error", attempt
            attempts[path] = attempt["attempt_id"]
            if attempt["phase"] != "parse":
                break
    for _ in range(10000):
        state = review.service.advance(review.matter.matter_id, max_steps=128)
        if state["state"] != "pending":
            assert state["state"] == ("error" if expected_failed else "complete"), state
            for path, attempt_id in attempts.items():
                final = review.repository.attempt(review.matter.matter_id, attempt_id)
                assert final["status"] == ("failed" if path in expected_failed else "complete"), final
            return attempts
    pytest.fail("Production work did not complete within the generated fixture's step envelope")


def record_findings(record):
    """Parser evidence and derived association findings have separate durable fields."""
    return set(record["diagnostics"]) | set(record["association_diagnostics"]) | {record["association_status"]}


def all_records(review, attempt_id, category="document"):
    after = 0
    while True:
        rows = review.repository.records(review.matter.matter_id, attempt_id,
                                         category=category, after=after, limit=100)
        if not rows:
            return
        assert len(rows) <= 100 and rows[0]["ordinal"] > after
        yield from rows
        after = rows[-1]["ordinal"]


def record_request(review, path, attempt, ordinal, *, category="opt", direction="at", endpoint="full", **query):
    params = {"doc": review.token(path), "view": "document" if category == "opt" else "fields",
              "record": f"{category}:{attempt}:{ordinal}", "record_dir": direction, **query}
    url = review.base if endpoint == "full" else review.base + "/items/" + params.pop("doc")
    return review.client.get(url, params=params)


def record_article(response, ordinal):
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    match = re.search(r'<article\b[^>]*data-record-ordinal="' + str(ordinal) + r'"[^>]*>(.*?)</article>',
                      response.text, re.S)
    assert match is not None, response.text
    return html.unescape(re.sub(r"<[^>]+>", " ", match.group(1)))


def test_dat_loadfile_shape_preserves_cells_and_lines(tmp_path):
    headers = ("BEGBATES", "ENDBATES", "NATIVEPATH", "BEGATTACH", "ENDATTACH", "Arbitrary supplied note")
    long_value = "  Generated ® text " + "unclipped" * 800 + "\r\nsecond physical line  "
    values = ("  SYN0001 ", "SYN0002", "Generated/native.txt", "SYN0001", "SYN0002", long_value)
    path = tmp_path / "generated.dat"
    path.write_bytes(dat_bytes(headers, [values, ("SYN0003", "SYN0003", "other.txt", "", "", "tail")]))
    definition = replace(fields.load_definition(), default_sort="name")
    events = []
    table = load_files.parse(path, document_id="1" * 32, name="generated.dat", definition=definition,
                             on_event=events.append)
    assert table is not None and not isinstance(table.rows, (list, tuple))
    rows = list(table.rows)
    assert rows[0].raw_headers == headers and rows[0].raw_cells == values
    assert rows[0].values == dict(zip(headers, values))
    assert [row.line for row in rows] == [2, 4]
    assert [row.ordinal for row in rows] == [1, 2]
    assert table.complete and not table.problem
    assert table.accepted_count == table.record_count == 2 and table.rejected_count == 0
    assert table.bates_header == "BEGBATES" and table.path_header == "NATIVEPATH"
    assert [event.kind for event in events] == ["header", "accepted", "accepted"]
    ambiguous = tmp_path / "ambiguous.dat"
    ambiguous.write_bytes(dat_bytes(("BEGBATES", "BEGIN BATES", "NATIVEPATH"),
                                   [("SYN1", "OTHER1", "native.txt")]))
    table = load_files.parse(ambiguous, document_id="2" * 32, name="ambiguous.dat", definition=definition)
    row = next(iter(table.rows))
    assert row.raw_cells == ("SYN1", "OTHER1", "native.txt")
    assert table.bates_header == "" and table.ambiguous_headers


def test_dat_adapter_encoding_variants(tmp_path):
    headers = ("BEGBATES", "NATIVEPATH", "Note")
    values = ("SYN0001", "Generated/café.txt", "  literal ® and café\r\ncontinuation  ")
    for encoding in ("utf-8", "utf-8-sig", "cp1252", "utf-16-le", "utf-16-be"):
        path = tmp_path / (encoding + ".dat")
        path.write_bytes(dat_bytes(headers, [values], encoding=encoding))
        table = load_files.parse(path, document_id="3" * 32, name=path.name, definition=fields.load_definition())
        rows = tuple(table.rows)
        assert rows[0].raw_headers == headers and rows[0].raw_cells == values
        assert rows[0].line == 2 and table.complete and not table.problem
    valid = dat_bytes(headers, [values])
    ascii_prefix = dat_bytes(headers, [("SYN0001", "native.txt", "plain")] * 3000)
    assert len(ascii_prefix) > 65536
    invalid = [valid.decode().encode("utf-32"), valid.decode().encode("utf-16-le"),
               ascii_prefix + b"\xff\x14value\x14tail\n", valid + b"one\x14two\n",
               b"BEGBATES\x14NATIVEPATH\n\xc3\xbeinner\xc3\xbethorn\xc3\xbe\x14native.txt\n",
               b"BEGBATES\x14NATIVEPATH\n" + b"x" * (1024 * 1024 + 1) + b"\x14native.txt\n"]
    for index, body in enumerate(invalid):
        path = tmp_path / f"invalid-{index}.dat"
        path.write_bytes(body)
        events = []
        table = load_files.parse(path, document_id="4" * 32, name=path.name,
                                 definition=fields.load_definition(), on_event=events.append)
        tuple(table.rows)
        assert table.problem and any(event.kind in {"rejected", "file_failure"} for event in events)
        assert any(event.diagnostics for event in events if event.kind in {"rejected", "file_failure"})


@pytest.mark.parametrize("reverse", [False, True], ids=["forward", "reverse"])
@pytest.mark.parametrize("equivalent", [True, False], ids=["equivalent", "conflicting"])
def test_dat_projection_cardinality_is_order_independent(tmp_path, reverse, equivalent):
    headers = ("BEGBATES", "ENDBATES", "NATIVEPATH", "Note")
    first = ("SYN0001", "SYN0001", "Generated/native.txt", "exact raw extra")
    second = first if equivalent else (*first[:3], "different raw extra")
    rows = [second, first] if reverse else [first, second]
    runtime = tmp_path / "runtime"
    with opened_review(runtime) as review:
        upload_production(review, {"Generated/native.txt": source_text(1),
            "Generated/load.dat": dat_bytes(headers, rows)})
        attempt = import_paths(review, ["Generated/load.dat"])["Generated/load.dat"]
        projected = review.projection("Generated/native.txt")
        records = tuple(all_records(review, attempt))
        assert len(records) == 2 and {record["line"] for record in records} == {2, 3}
        assert [tuple(record["cells"]) for record in records] == rows
        assert "duplicate_begin" in {code for record in records for code in record_findings(record)}
        if equivalent:
            assert projected["values"]["begin_bates"] == "SYN0001"
            assert tuple(projected["raw_fields"])[-1] == ("Note", "exact raw extra")
            assert any(reference["attempt_id"] == attempt for reference in projected["references"])
        else:
            assert "begin_bates" not in projected["values"]
            assert "conflicting_dat_rows" in projected["diagnostics"]
            assert not projected["family"]
            assert any(reference["attempt_id"] == attempt for reference in projected["references"])
            response = review.client.get(review.base + "/items/" + review.token("Generated/native.txt"),
                                         params={"view": "fields"})
            assert response.status_code == 200 and "conflicting_dat_rows" in response.text
            source_links = [parse_qs(urlsplit(html.unescape(url)).query)
                            for url in re.findall(r'href="([^"]+)"', response.text)]
            assert any(link.get("doc") == [review.token("Generated/load.dat")]
                       and link.get("view") == ["fields"] for link in source_links)
        slug = review.matter.slug
        expected = (projected["values"], projected["diagnostics"])
    with opened_review(runtime, slug) as review:
        import_paths(review, ["Generated/load.dat"])
        projected = review.projection("Generated/native.txt")
        assert (projected["values"], projected["diagnostics"]) == expected
        assert len(tuple(all_records(review, attempt))) == 2


@pytest.mark.parametrize("order", [("csv", "dat"), ("dat", "csv")])
@pytest.mark.parametrize("extra", ["compatible", "conflicting", "incomplete"])
def test_dat_csv_conflicts_are_order_independent(review, order, extra):
    legacy_value = "shared" if extra == "compatible" else "different"
    csv_data = f"BEGBATES,NATIVEPATH,Note,CSV only\nSYN0001,Generated/native.txt,{legacy_value},legacy retained\n".encode()
    if extra == "incomplete":
        csv_data += b"SYN0002,missing-extra\n"
    files = {"Generated/native.txt": source_text(1), "Generated/load.csv": csv_data,
        "Generated/load.dat": dat_bytes(("BEGBATES", "NATIVEPATH", "Note", "DAT only"),
            [("SYN0001", "Generated/native.txt", "shared", "retained")])}
    upload_production(review, files)
    import_paths(review, ["Generated/load." + suffix for suffix in order],
                 expected_failed=("Generated/load.csv",) if extra == "incomplete" else ())
    projected = review.projection("Generated/native.txt")
    if extra == "compatible":
        assert projected["values"]["begin_bates"] == "SYN0001"
        assert ("DAT only", "retained") in tuple(projected["raw_fields"])
    else:
        assert "conflicting_load_file_metadata" in projected["diagnostics"]
        assert ("DAT only", "retained") not in tuple(projected["raw_fields"])
    page = review.client.get(review.base + "/items/" + review.token("Generated/native.txt"), params={"view": "fields"})
    assert page.status_code == 200
    assert legacy_value in html.unescape(page.text)
    assert "legacy retained" in html.unescape(page.text)
    assert ("conflicting_load_file_metadata" in page.text or "conflicting load-file metadata" in page.text.lower()) is (extra != "compatible")


def test_no_opt_has_no_page_bates(review):
    upload_production(review, {"Generated/native.txt": source_text(1),
        "Generated/load.dat": dat_bytes(("BEGBATES", "ENDBATES", "NATIVEPATH", "PAGECOUNT"),
            [("SYN0001", "SYN1000", "Generated/native.txt", "1000")])})
    import_paths(review, ["Generated/load.dat"])
    for endpoint in (review.base, review.base + "/items/" + review.token("Generated/native.txt")):
        response = review.client.get(endpoint, params={"doc": review.token("Generated/native.txt"), "view": "document"})
        assert response.status_code == 200
        assert "data-record-kind=\"opt\"" not in response.text
        assert "Supplied page Bates" not in response.text
        assert "SYN0002" not in response.text and "SYN0999" not in response.text
    assert review.projection("Generated/native.txt")["values"]["load_file_pages"] == "1000"


@pytest.mark.parametrize("case,expected", [
    ("exact", "Generated/a.txt"), ("filename", "Generated/a.txt"),
    ("bates", "Generated/b.txt"), ("control", "Generated/b.txt"),
    ("bates-name", "Generated/SYN0099.txt"), ("contradictory", ""),
    ("ambiguous", ""), ("unsafe", ""), ("team-id", ""), ("foreign-root", ""),
])
def test_scoped_matching_by_path_bates_and_control(review, case, expected):
    keys = {
        "exact": ("SYN0001", "CTRL0001", "Generated/a.txt"),
        "filename": ("SYN0001", "CTRL0001", "a.txt"),
        "bates": ("SYN0002", "", ""), "control": ("", "CTRL0002", ""),
        "bates-name": ("SYN0099", "", ""),
        "contradictory": ("SYN0002", "CTRL0002", "Generated/a.txt"),
        "ambiguous": ("", "", "same.txt"), "unsafe": ("", "", "../a.txt"),
        "team-id": ("TEAM0001", "", ""), "foreign-root": ("SYN0088", "", "Other/native.txt"),
    }
    supplied = tuple((header, value) for header, value in zip(
        ("BEGBATES", "CONTROLNUMBER", "NATIVEPATH"), keys[case]) if value)
    first_legacy_path = "a.txt" if case == "filename" else "Generated/a.txt"
    legacy = ("BEGBATES,CONTROLNUMBER,NATIVEPATH\nSYN0001,CTRL0001," + first_legacy_path
              + "\nSYN0002,CTRL0002,Generated/b.txt\n").encode()
    files = {"Generated/a.txt": source_text(1), "Generated/b.txt": source_text(2),
        "Generated/SYN0099.txt": source_text(99), "Generated/one/same.txt": source_text(3),
        "Generated/two/same.txt": source_text(4),
        "Generated/legacy.csv": legacy,
        "Generated/load.dat": dat_bytes(tuple(header for header, _ in supplied) + ("DAT witness",),
            [tuple(value for _, value in supplied) + ("exact supplied marker",)])}
    upload_production(review, files)
    if case == "foreign-root":
        upload_production(review, {"Other/native.txt": source_text(88)})
    identifiers = IdentifierRepository(review.repository.db, review.bench.workspace.membership)
    if case == "team-id":
        assert identifiers.assign(review.matter.matter_id, ACTOR,
            [(review.document("Generated/a.txt").document_id, "TEAM0001")]) == 1
    attempt = import_paths(review, ["Generated/legacy.csv", "Generated/load.dat"])["Generated/load.dat"]
    records = tuple(all_records(review, attempt))
    assert len(records) == 1 and tuple(records[0]["cells"]) == tuple(value for _, value in supplied) + ("exact supplied marker",)
    if expected:
        assert records[0]["document_id"] == review.document(expected).document_id
        assert ("DAT witness", "exact supplied marker") in tuple(review.projection(expected)["raw_fields"])
    else:
        assert records[0]["association_status"] != "matched"
        for path in ("Generated/a.txt", "Generated/b.txt", "Generated/SYN0099.txt",
                     "Generated/one/same.txt", "Generated/two/same.txt"):
            assert ("DAT witness", "exact supplied marker") not in tuple(review.projection(path)["raw_fields"])
        diagnostics = record_findings(records[0])
        if case == "contradictory":
            assert "conflicting_match" in diagnostics
        elif case == "ambiguous":
            assert "ambiguous_match" in diagnostics
        elif case == "unsafe":
            assert any("path" in code for code in diagnostics)
        elif case == "team-id":
            assert identifiers.read(review.matter.matter_id)[review.document("Generated/a.txt").document_id] == "TEAM0001"


def test_unmatched_dat_rows_remain_visible_without_sources(review):
    headers = ("BEGBATES", "NATIVEPATH", "Note")
    rows = [(f"SYN{number:04}", f"Generated/native-{number}.txt", f"supplied row {number}")
            for number in range(1, 126)]
    files = {"Generated/load.dat": dat_bytes(headers, rows), "Generated/native-125.txt": source_text(125)}
    receipt = upload_production(review, files, eligible=[0])
    attempt = import_paths(review, ["Generated/load.dat"])["Generated/load.dat"]
    assert len(review.store.documents) == 1
    stored = tuple(all_records(review, attempt))
    assert len(stored) == 125 and all(record["association_status"] == "unmatched" for record in stored)
    totals = review.repository.attempt(review.matter.matter_id, attempt)["counts"]
    assert totals["accepted"] == totals["unmatched"] == 125
    assert totals["matched"] == totals["rejected"] == totals["conflicted"] == 0
    response = record_request(review, "Generated/load.dat", attempt, 125, category="dat")
    shown = record_article(response, 125)
    assert "SYN0125" in shown and "supplied row 125" in shown
    assert "data-production-coverage" in response.text
    assert len(review.store.documents) == 1
    # This explicit stored-event fixture proves rejected-record visibility, not admission of malformed bytes.
    review.repository.update_attempt(review.matter.matter_id, attempt, status="building", phase="complete")
    review.repository.append_events(review.matter.matter_id, attempt, [RecordEvent(
        kind="rejected", category="document", source_format="dat", ordinal=126, line=200,
        headers=headers, cells=None, diagnostics=("field_count_mismatch",))])
    review.repository.update_attempt(review.matter.matter_id, attempt, status="complete", phase="complete",
        counts={"accepted": 125, "rejected": 1, "matched": 0, "unmatched": 125, "conflicted": 0})
    rejected = record_request(review, "Generated/load.dat", attempt, 126, category="dat")
    shown = record_article(rejected, 126)
    assert "Rejected" in shown and "field_count_mismatch" in shown and "200" in shown
    assert "could not be recovered" in shown
    upload_production(review, files, eligible=[1], receipt=receipt)
    import_paths(review, ["Generated/load.dat"])
    assert len(review.store.documents) == 2
    projected = review.projection("Generated/native-125.txt")
    assert projected["values"]["begin_bates"] == "SYN0125"
    current = review.repository.current_attempt(review.matter.matter_id,
                                               review.document("Generated/load.dat").document_id)
    assert current["counts"]["accepted"] == 125
    assert current["counts"]["matched"] == 1 and current["counts"]["unmatched"] == 124


def small_page_production(review):
    pages = [("SYN0001", "VOL", "Generated/images/one.tif", "Y", "raw fifth", "raw sixth", "3"),
             ("PAGE-Z09", "VOL", "Generated/images/two.tif", "", "", "", ""),
             ("PAGE®-X07", "OTHER-VOL", "Generated/images/three.tif", "", "fifth tail", "sixth tail", "")]
    upload_production(review, {"Generated/native.txt": source_text(1),
        "Generated/load.dat": dat_bytes(("BEGBATES", "ENDBATES", "NATIVEPATH"),
            [("SYN0001", "SYN0003", "Generated/native.txt")]), "Generated/pages.opt": opt_bytes(pages)})
    attempts = import_paths(review, ["Generated/load.dat", "Generated/pages.opt"])
    return attempts, pages


def test_opt_page_records_are_exact_and_document_only(review):
    attempts, pages = small_page_production(review)
    attempt = attempts["Generated/pages.opt"]
    stored = tuple(all_records(review, attempt, "page"))
    assert [tuple(record["cells"]) for record in stored] == pages
    assert [record["line"] for record in stored] == [1, 2, 3]
    source = review.document("Generated/pages.opt")
    for endpoint in ("full", "fragment"):
        response = record_request(review, "Generated/native.txt", attempt, 3, endpoint=endpoint)
        shown = record_article(response, 3)
        assert all(value in shown for value in pages[2] if value)
        assert "Generated/pages.opt" in shown and source.version_id in shown
        assert "record" in shown.casefold() and "line" in shown.casefold()
        assert "unavailable" in response.text.casefold()
        assert "#page=3" not in response.text
        assert 'data-record-direction="next"' not in response.text
    for view in ("text", "fields"):
        response = review.client.get(review.base + "/items/" + review.token("Generated/native.txt"), params={"view": view})
        assert response.status_code == 200
        assert "PAGE-Z09" not in response.text and "PAGE®-X07" not in html.unescape(response.text)
        assert "Supplied page Bates" not in response.text
    listing = review.client.get(review.base)
    table = re.search(r"<tbody>(.*?)</tbody>", listing.text, re.S).group(1)
    assert "PAGE-Z09" not in table and "PAGE®-X07" not in html.unescape(table)


def test_opt_bad_breaks_and_unmatched_pages_are_reported(review):
    pages = [("ORPHAN1", "VOL", "Generated/missing.tif", "", "", "", ""),
             ("SYN0001", "VOL", "Generated/one.tif", "Y", "", "", "2"),
             ("BROKEN1", "VOL", "Generated/two.tif", "N", "", "", ""),
             ("AFTER1", "VOL", "Generated/three.tif", "", "", "", ""),
             ("SYN0099", "VOL", "Generated/four.tif", "Y", "", "", "1")]
    upload_production(review, {"Generated/native.txt": source_text(1),
        "Generated/load.dat": dat_bytes(("BEGBATES", "NATIVEPATH"), [("SYN0001", "Generated/native.txt")]),
        "Generated/pages.opt": opt_bytes(pages)})
    attempts = import_paths(review, ["Generated/load.dat", "Generated/pages.opt"])
    records = tuple(all_records(review, attempts["Generated/pages.opt"], "page"))
    assert len(records) == 5 and [tuple(record["cells"]) for record in records] == pages
    assert records[0]["association_status"] != "matched"
    assert "invalid_document_break" in record_findings(records[2])
    assert records[3]["association_status"] != "matched"
    assert records[4]["association_status"] != "matched"
    for ordinal in (1, 3, 4, 5):
        response = record_request(review, "Generated/pages.opt", attempts["Generated/pages.opt"], ordinal)
        assert pages[ordinal - 1][0] in record_article(response, ordinal)


def synthetic_tail_generation(review, count=1_000_002):
    """Persist synthetic completed records; this is not a parser or digest-validation claim."""
    attempts, _ = small_page_production(review)
    attempt_id = attempts["Generated/pages.opt"]
    source = review.document("Generated/pages.opt")
    native = review.document("Generated/native.txt")
    attempt = review.repository.attempt(review.matter.matter_id, attempt_id)
    workspace = review.bench.workspace
    with fixture_write(workspace) as connection:
        connection.execute("DELETE FROM workbench_discovery_opt_page WHERE matter_id=? AND attempt_id=?",
                           (review.matter.matter_id, attempt_id))
        for start in range(1, count + 1, 256):
            batch = []
            for ordinal in range(start, min(start + 256, count + 1)):
                label = "TAIL®-X0007" if ordinal == count else ("SYN0001" if ordinal == 1 else f"PAGE{ordinal}")
                volume = "TAIL-VOLUME" if ordinal == count else "VOL"
                path = "Generated/images/tail-distinct.tif" if ordinal == count else f"Generated/images/p{ordinal}.tif"
                cells = (label, volume, path, "Y" if ordinal == 1 else "", "", "", str(count) if ordinal == 1 else "")
                batch.append((review.matter.matter_id, attempt_id, ordinal, ordinal, "accepted", "[]",
                    json.dumps(cells, ensure_ascii=False), "[]", "[]", hashlib.sha256(str(ordinal).encode()).hexdigest(),
                    label.casefold(), path.casefold(), native.document_id, "synthetic completed fixture", "matched",
                    attempt["generation"], 1, "syn0001", cells[6], count))
            connection.executemany(
                "INSERT INTO workbench_discovery_opt_page(matter_id,attempt_id,ordinal,line,kind,headers_json,"
                "cells_json,diagnostics_json,optional_values_json,raw_fingerprint,page_label_key,path_key,"
                "document_id,basis,association_status,generation,group_ordinal,group_key,supplied_count,observed_count) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", batch)
    review.repository.update_attempt(review.matter.matter_id, attempt_id,
        status="complete", phase="complete", counts={"accepted": count, "rejected": 0,
            "matched": count, "unmatched": 0, "conflicted": 0})
    last = review.repository.record(review.matter.matter_id, attempt_id, count, category="page")
    assert last["source_document_id"] == source.document_id and last["source_version_id"] == source.version_id
    assert last["ordinal"] == last["line"] == count and last["cells"][0] == "TAIL®-X0007"
    assert review.repository.attempt(review.matter.matter_id, attempt_id)["counts"]["accepted"] == count
    return attempt_id


def test_opt_navigation_reaches_tail_on_both_endpoints(review):
    attempt = synthetic_tail_generation(review)
    source = review.document("Generated/pages.opt")
    for selected in ("Generated/native.txt", "Generated/pages.opt"):
        for endpoint in ("full", "fragment"):
            for current, direction, expected in ((1_000_000, "at", 1_000_000),
                    (1_000_000, "next", 1_000_001), (1_000_001, "next", 1_000_002),
                    (1_000_002, "previous", 1_000_001), (1_000_001, "previous", 1_000_000)):
                statements = []
                workspace = review.bench.workspace
                with fixture_write(workspace) as connection:
                    connection.set_trace_callback(statements.append)
                try:
                    response = record_request(review, selected, attempt, current,
                        direction=direction, endpoint=endpoint)
                finally:
                    with fixture_write(workspace) as connection:
                        connection.set_trace_callback(None)
                shown = record_article(response, expected)
                if expected == 1_000_002:
                    assert "TAIL®-X0007" in shown and "TAIL-VOLUME" in shown
                    assert "Generated/images/tail-distinct.tif" in shown and source.version_id in shown
                    assert 'data-record-direction="next"' not in response.text
                else:
                    assert f"PAGE{expected}" in shown
                    assert 'data-record-direction="next"' in response.text
                for link_direction, adjacent in (("previous", expected - 1), ("next", expected + 1)):
                    if adjacent > 1_000_002:
                        continue
                    tag = re.search(r'<a\b[^>]*data-record-direction="' + link_direction + r'"[^>]*>', response.text)
                    assert tag is not None
                    for attribute in ("href", "data-review-fragment"):
                        value = html.unescape(re.search(attribute + r'="([^"]+)"', tag.group()).group(1))
                        query = parse_qs(urlsplit(value).query)
                        assert query["record"] == [f"opt:{attempt}:{adjacent}"]
                        assert query["view"] == ["document"] and "unit" not in query
                        if attribute == "href":
                            assert query["doc"] == [review.token(selected)]
                page_reads = [sql for sql in statements if sql.lstrip().upper().startswith("SELECT")
                              and "workbench_discovery_opt_page" in sql]
                assert page_reads
                for sql in page_reads:
                    upper = sql.upper()
                    assert "OFFSET" not in upper and re.search(r"LIMIT\s+1(?:\D|$)", upper), sql
                    plan = review.repository.explain_read(sql)
                    descriptions = " ".join(str(row[3]) for row in plan).upper()
                    assert "SEARCH" in descriptions and "SCAN" not in descriptions, descriptions


def test_record_cursor_validation_and_matter_isolation(review):
    files = {"Generated/one.txt": source_text(1), "Generated/two.txt": source_text(2),
        "Generated/load.dat": dat_bytes(("BEGBATES", "NATIVEPATH"),
            [("SYN0001", "Generated/one.txt"), ("SYN0002", "Generated/two.txt")]),
        "Generated/pages.opt": opt_bytes([
            ("SYN0001", "V", "Generated/one.tif", "Y", "", "", "1"),
            ("SYN0002", "V", "Generated/two.tif", "Y", "", "", "1")])}
    upload_production(review, files)
    attempts = import_paths(review, ["Generated/load.dat", "Generated/pages.opt"])
    attempt = attempts["Generated/pages.opt"]
    native_token = review.token("Generated/one.txt")
    valid = f"opt:{attempt}:1"
    malformed = ["opt:bad:1", f"OPT:{attempt}:1", f"opt:{attempt.upper()}:1", f"opt:{attempt}:0",
        f"opt:{attempt}:01", f"opt:{attempt}:-1", f"opt:{attempt}:1.0", f"opt:{attempt}:{MAX_ORDINAL + 1}",
        f"opt:{attempt}:" + "9" * 10000, valid + ":extra", " " + valid]
    for endpoint in (review.base, review.base + "/items/" + native_token):
        common = {"doc": native_token, "view": "document", "record": valid}
        response = review.client.get(endpoint, params=common)
        assert "SYN0001" in record_article(response, 1)
        for cursor in malformed:
            assert review.client.get(endpoint, params={**common, "record": cursor}).status_code == 422
        for changes in ({"record_dir": "sideways"}, {"record": "", "record_dir": "next"},
                        {"view": "fields"}, {"record": f"dat:{attempt}:1", "view": "document"}):
            assert review.client.get(endpoint, params={**common, **changes}).status_code == 422
        dat_cursor = "dat:" + attempts["Generated/load.dat"] + ":1"
        for options in ({"image_start": 1}, {"view": "fields", "record": "", "image_start": 1},
                        {"view": "fields", "record": dat_cursor, "image_start": 1, "record_dir": "next"},
                        {"view": "fields", "record": dat_cursor, "image_start": 1},
                        {"view": "fields", "record": dat_cursor, "image_start": 0}):
            assert review.client.get(endpoint, params={**common, **options}).status_code == 422
        assert review.client.get(endpoint, params={**common, "record": f"opt:{attempt}:{MAX_ORDINAL}"}).status_code == 404
        assert review.client.get(endpoint, params={**common, "record": f"opt:{attempt}:2"}).status_code == 404
        assert review.client.get(endpoint, params={**common, "record": f"opt:{'0' * 32}:1"}).status_code == 404
    assert review.client.get(review.base, params={"view": "document", "record": valid}).status_code == 422
    assert review.client.get(review.base, params={"doc": native_token, "view": "document",
        "record": valid, "q": "no-generated-record-matches-this"}).status_code == 404
    assert record_request(review, "Generated/load.dat", attempt, 1).status_code == 404
    neighbor = review.bench.create_matter("Generated neighbor", "", ACTOR)
    neighbor_review = review_for(review.client, neighbor)
    upload_production(neighbor_review, {"Other/native.txt": source_text(99)})
    for endpoint in (neighbor_review.base,
                     neighbor_review.base + "/items/" + neighbor_review.token("Other/native.txt")):
        response = review.client.get(endpoint, params={"doc": neighbor_review.token("Other/native.txt"),
            "view": "document", "record": valid})
        assert response.status_code == 404 and "SYN0001" not in response.text
    review.repository.update_attempt(review.matter.matter_id, attempt, status="stale", phase="complete")
    for endpoint in ("full", "fragment"):
        stale = record_request(review, "Generated/one.txt", attempt, 1, endpoint=endpoint)
        assert stale.status_code == 409 and "reload" in stale.text.casefold()
    assert review.client.get(neighbor_review.base, params={"doc": neighbor_review.token("Other/native.txt"),
        "view": "document", "record": valid}).status_code == 404
    workspace = review.bench.workspace
    owner = workspace.upsert_principal("test", "generated-record-owner", "Generated owner", "generated")
    shared = workspace.create_matter("Generated shared records", "", owner.principal_id)
    workspace.add_member(shared.matter_id, ACTOR, owner.principal_id)
    shared_review = review_for(review.client, shared)
    shared_attempts, _ = small_page_production(shared_review)
    shared_attempt = shared_attempts["Generated/pages.opt"]
    assert record_request(shared_review, "Generated/native.txt", shared_attempt, 1).status_code == 200
    workspace.revoke_member(shared.matter_id, ACTOR, owner.principal_id)
    for endpoint in ("full", "fragment"):
        denied = record_request(shared_review, "Generated/native.txt", shared_attempt, 1, endpoint=endpoint)
        assert denied.status_code == 404 and "SYN0001" not in denied.text


def test_other_position_queries_have_no_million_ceiling(review):
    upload_production(review, {"Generated/native.txt": source_text(1)})
    token = review.token("Generated/native.txt")
    for value in (1_000_001, MAX_ORDINAL):
        full = review.client.get(review.base, params={"doc": token, "view": "text", "unit": value})
        fragment = review.client.get(review.base + "/items/" + token, params={"view": "text", "unit": value})
        page = review.client.get(review.base, params={"page": value})
        assert full.status_code == fragment.status_code == page.status_code == 200
        assert "Generated source 1 carries searchable copper text." in full.text
        assert "Generated source 1 carries searchable copper text." in fragment.text
    for value in ("0", "-1", str(MAX_ORDINAL + 1), "9" * 10000, "1.5", "nan"):
        assert review.client.get(review.base, params={"page": value}).status_code == 422
        assert review.client.get(review.base, params={"doc": token, "view": "text", "unit": value}).status_code == 422
        assert review.client.get(review.base + "/items/" + token, params={"view": "text", "unit": value}).status_code == 422


def row_tokens(text):
    return re.findall(r'<tr\b[^>]*data-review-row[^>]*data-token="([0-9a-f]{32})"', text)


def complete_listing(review, **params):
    from tests.test_discovery_review_index import service_for
    service = service_for(review.bench)
    for unused in range(200):
        response = review.client.get(review.base, params=params)
        assert response.status_code == 200
        state = service.repository.state(review.matter.matter_id)
        assert not service.jobs[review.matter.matter_id].error, getattr(service, 'failure', '')
        if state and state['phase'] == 'complete':
            assert not state['pending'] and not state['errors']
            return response
    pytest.fail('The bounded stored index did not finish the synthetic list')


def test_family_index_paging_filter_and_invalidation(review):
    size = 106
    files = {"Generated/z-parent.txt": source_text(1), "Generated/a-independent.txt": source_text(900)}
    rows = [("SYN0001", "SYN0001", "Generated/z-parent.txt", "SYN0001", f"SYN{size:04}", "parent")]
    for ordinal in range(2, size + 1):
        name = "needle-child.txt" if ordinal == 2 else f"child-{ordinal:04}.txt"
        path = "Generated/" + name
        files[path] = source_text(ordinal)
        rows.append((f"SYN{ordinal:04}", f"SYN{ordinal:04}", path, "SYN0001", f"SYN{size:04}", "child"))
    files["Generated/families.dat"] = dat_bytes(
        ("BEGBATES", "ENDBATES", "NATIVEPATH", "BEGATTACH", "ENDATTACH", "Note"), list(reversed(rows)))
    upload_production(review, files)
    import_paths(review, ["Generated/families.dat"])
    parent = review.document("Generated/z-parent.txt")
    child = review.document("Generated/needle-child.txt")
    parent_projection = review.projection("Generated/z-parent.txt")
    assert parent_projection["family"]["root"] == parent.document_id
    assert review.projection("Generated/needle-child.txt")["family"]["root"] == parent.document_id
    for direction in ("asc", "desc"):
        from tests.test_discovery_review_index import page_link
        first = complete_listing(review, sort="name", dir=direction)
        link = page_link(first.text, 'next')
        assert link
        pages = [first, review.client.get(link)]
        assert all(response.status_code == 200 for response in pages)
        tokens = [row_tokens(response.text) for response in pages]
        assert all(len(page) <= 100 for page in tokens)
        flattened = [token for page in tokens for token in page]
        assert len(flattened) == len(set(flattened)) == len(files)
        parent_position = flattened.index(review.store.action_token(parent))
        child_positions = [flattened.index(review.token(path)) for path in files
                           if path.startswith("Generated/child-") or path == "Generated/needle-child.txt"]
        assert all(parent_position < position for position in child_positions)
        family_tokens = [review.store.action_token(parent)] + [
            review.token("Generated/needle-child.txt" if number == 2 else f"Generated/child-{number:04}.txt")
            for number in range(2, size + 1)]
        assert [token for token in flattened if token in set(family_tokens)] == family_tokens
        assert "data-family-context" in pages[1].text
    filtered = review.client.get(review.base, params={"q": "needle-child", "sort": "name", "dir": "desc"})
    assert filtered.status_code == 200
    assert row_tokens(filtered.text) == [review.store.action_token(child)]
    final_window = filtered
    while link := page_link(final_window.text, 'next'):
        final_window = review.client.get(link)
        assert final_window.status_code == 200
    assert "1 matches the filter" in final_window.text and "data-family-context" in filtered.text
    assert "z-parent.txt" in filtered.text
    assigned = review.client.post(review.base + "/identifiers", data={"q": "needle-child", "sort": "name",
        "dir": "desc", "include_produced": "1", "prefix": "TEAM", "start": 1, "digits": 6}, follow_redirects=False)
    assert assigned.status_code == 303 and "notice=assigned" in assigned.headers["location"]
    identifiers = IdentifierRepository(review.repository.db, review.bench.workspace.membership)
    assert identifiers.read(review.matter.matter_id) == {child.document_id: "TEAM000001"}
    terminal = review.document(f"Generated/child-{size:04}.txt")
    removed = review.client.post(f"/matters/{review.matter.slug}/sources/{review.store.action_token(terminal)}/remove",
                                follow_redirects=False)
    assert removed.status_code == 303
    import_paths(review, ["Generated/families.dat"])
    after = review.projection("Generated/z-parent.txt")
    assert not any(after["family"].get(key) for key in ("root", "parent", "order"))
    assert "orphan_attachment" in after["diagnostics"]
    current = review.repository.current_attempt(review.matter.matter_id,
        review.document("Generated/families.dat").document_id)
    assert current["counts"]["accepted"] == size
    assert len(tuple(all_records(review, current["attempt_id"]))) == size
    assert identifiers.read(review.matter.matter_id) == {child.document_id: "TEAM000001"}


@pytest.mark.parametrize("first", ["import", "identifier"])
def test_team_identifier_import_race_preserves_both_inputs(review, monkeypatch, first):
    upload_production(review, {"Generated/one.txt": source_text(1), "Generated/two.txt": source_text(2),
        "Generated/three.txt": source_text(3),
        "Generated/load.dat": dat_bytes(("BEGBATES", "CONTROLNUMBER", "NATIVEPATH"),
            [("TEAM0001", "CONTROL0001", "Generated/one.txt")])})
    identifiers = IdentifierRepository(review.repository.db, review.bench.workspace.membership)
    document_ids = [review.document(f"Generated/{name}.txt").document_id for name in ("one", "two", "three")]
    checks = {"import": [], "identifier": []}
    first_entered, release_first, second_started = (threading.Event() for _ in range(3))
    original_team = ProductionRepository.team_conflicts
    original_produced = ProductionRepository.identifier_conflicts

    def transaction_boundary(operation, in_transaction):
        checks[operation].append(in_transaction)
        if operation == first and in_transaction and not first_entered.is_set():
            first_entered.set()
            assert release_first.wait(10), "The overlapping operation did not reach the transaction boundary"

    def checked_team(repository, matter_id, labels):
        transaction_boundary("import", repository.db.connection.in_transaction)
        return original_team(repository, matter_id, labels)

    def checked_produced(repository, matter_id, labels, *, connection=None):
        transaction_boundary("identifier", (connection or repository.db.connection).in_transaction)
        return original_produced(repository, matter_id, labels, connection=connection)

    monkeypatch.setattr(ProductionRepository, "team_conflicts", checked_team)
    monkeypatch.setattr(ProductionRepository, "identifier_conflicts", checked_produced)

    def import_operation():
        return import_paths(review, ["Generated/load.dat"])

    def assignment_operation():
        pairs = ([(document_ids[1], "TEAM0001")] if first == "identifier" else
                 [(document_ids[1], "UNIQUE0001"), (document_ids[2], "TEAM0001")])
        return identifiers.assign(review.matter.matter_id, ACTOR, pairs)

    def overlapping_operation(operation):
        second_started.set()
        return operation()

    first_operation = assignment_operation if first == "identifier" else import_operation
    second_operation = import_operation if first == "identifier" else assignment_operation
    with ThreadPoolExecutor(max_workers=2) as workers:
        first_result = workers.submit(first_operation)
        try:
            assert first_entered.wait(10), "The first operation did not perform its transactional collision check"
            second_result = workers.submit(overlapping_operation, second_operation)
            assert second_started.wait(10), "The other write did not start while the first transaction was held"
        finally:
            release_first.set()
        first_value = first_result.result(timeout=20)
        if first == "identifier":
            assert first_value == 1
            attempts = second_result.result(timeout=20)
            projected = review.projection("Generated/one.txt")
            assert "team_identifier_conflict" in projected["diagnostics"]
            assert "begin_bates" not in projected["values"]
            assert identifiers.read(review.matter.matter_id) == {document_ids[1]: "TEAM0001"}
        else:
            attempts = first_value
            with pytest.raises(IdentifierConflict):
                second_result.result(timeout=20)
            assert review.projection("Generated/one.txt")["values"]["begin_bates"] == "TEAM0001"
            assert identifiers.read(review.matter.matter_id) == {}
    assert any(checks["import"]), "Import must recheck Team IDs within its publication transaction"
    assert any(checks["identifier"]), "Assignment must query imported literals within its write transaction"
    assert identifiers.assign(review.matter.matter_id, ACTOR, [(document_ids[2], "TEAM0002")]) == 1
    records = tuple(all_records(review, attempts["Generated/load.dat"]))
    assert len(records) == 1 and tuple(records[0]["cells"]) == ("TEAM0001", "CONTROL0001", "Generated/one.txt")
    assert (review.store.files / review.document("Generated/load.dat").stored_name).read_bytes() == dat_bytes(
        ("BEGBATES", "CONTROLNUMBER", "NATIVEPATH"), [("TEAM0001", "CONTROL0001", "Generated/one.txt")])


def test_load_file_format_and_record_semantic_seams():
    """Adapters share format/category/page semantics without authorizing future formats."""
    assert load_files.source_format("Generated/input.DAT") == "dat"
    assert load_files.source_format("Generated/input.OPT") == "opt"
    assert load_files.source_format("Generated/input.CSV") == "csv"
    assert load_files.source_format("Generated/input.TSV") == "tsv"
    assert load_files.source_format("Generated/input.LFP") == "lfp"
    assert load_files.source_format("Generated/input.DII") == "dii"
    for name in ("unknown.xyz", "future.lfp.exe", "future.dii.bak", "input.dat.exe"):
        assert load_files.source_format(name) == ""
    assert load_files.record_category("dat") == "document"
    assert load_files.record_category("opt") == "page"
    assert load_files.record_category("lfp") == "page"
    assert load_files.record_category("dii") == "document"
    assert load_files.record_category("unknown") == ""
    cells = ("PAGE®-X07", "VOL", "Generated/images/literal.tif", "Y", "raw fifth", "raw sixth", "3")
    event = RecordEvent(kind="accepted", category="page", source_format="opt", ordinal=7,
                        line=9, headers=(), cells=cells, diagnostics=())
    semantics = load_files.record_semantics(event)
    assert {key: semantics[key] for key in
            ("page_label", "volume", "path", "document_break", "page_count", "valid")} == {
                "page_label": cells[0], "volume": "VOL", "path": cells[2],
                "document_break": True, "page_count": "3", "valid": True}
    stored = {"kind": event.kind, "category": event.category, "source_format": event.source_format,
              "ordinal": event.ordinal, "line": event.line, "headers": [], "cells": list(cells),
              "diagnostics": []}
    assert load_files.record_semantics(stored) == semantics
    continuing = load_files.record_semantics(replace(event, cells=(*cells[:3], "", *cells[4:])))
    assert continuing["valid"] and continuing["document_break"] is False
    for damaged in (cells[:6], ("", *cells[1:]), (*cells[:2], "", *cells[3:]),
                    (*cells[:3], "N", *cells[4:])):
        assert load_files.record_semantics(replace(event, cells=damaged))["valid"] is False


def test_document_comparison_policy_and_priority():
    assert load_files.comparison_policy("dat", "dat") == "exact"
    for left, right in (("dat", "csv"), ("csv", "dat"), ("dat", "tsv"),
                        ("csv", "tsv"), ("csv", "csv"), ("dii", "dat"), ("dat", "dii"), ("dii", "dii")):
        assert load_files.comparison_policy(left, right) == "compatible"
    for left, right in (("dat", "unknown"), ("dii", "unknown"), ("opt", "dat"), ("opt", "opt"),
                        ("lfp", "dat"), ("lfp", "dii"), ("lfp", "lfp"), ("lfp", "opt")):
        assert load_files.comparison_policy(left, right) == ""
    assert load_files.projection_priority("dat") < load_files.projection_priority("dii")
    assert load_files.projection_priority("dii") < load_files.projection_priority("csv")
    assert load_files.projection_priority("dat") < load_files.projection_priority("csv")
    assert load_files.projection_priority("dat") < load_files.projection_priority("tsv")
    assert load_files.projection_priority("unknown") > max(
        load_files.projection_priority(name) for name in ("dat", "opt", "csv", "tsv", "lfp", "dii"))


def test_candidate_limits_apply_after_source_and_ready_filters(tmp_path):
    """An own-source stem cannot consume one of the two ambiguity witnesses."""
    from io import BytesIO
    from pypdf import PdfWriter
    from tests.test_discovery_review_dat_opt_storage import finish_import, source_identity, storage_case

    pdf = BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    writer.write(pdf)
    supplied = dat_bytes(("BEGBATES", "Generated evidence"), [("A1", "must remain ambiguous")])
    with storage_case(tmp_path, count=1, extra_files=(("A1.dat", supplied),
            ("A1.txt", source_text(2)), ("A1.pdf", pdf.getvalue()))) as case:
        # The public historical manifest migration keeps these explicit IDs and
        # the bounded fixture receipt proves one root. No admission verdict is replaced.
        by_name = {document.display_name: document for document in case.store.documents.values()}
        source = by_name["A1.dat"]
        assert source.document_id < by_name["A1.txt"].document_id < by_name["A1.pdf"].document_id
        root_key = source_identity(source, case.repository, case.matter).root_key
        assert root_key and all(source_identity(by_name[name], case.repository, case.matter).root_key == root_key
                                for name in ("A1.txt", "A1.pdf"))
        finish_import(case)
        attempt = case.repository.current_attempt(case.matter.matter_id, source.document_id)
        assert attempt["status"] == "complete"
        records = case.repository.records(case.matter.matter_id, attempt["attempt_id"], category="document", limit=2)
        assert len(records) == 1 and records[0]["cells"] == ["A1", "must remain ambiguous"]
        assert records[0]["association_status"] == "ambiguous_match"
        assert not records[0]["document_id"]
        for name in ("A1.txt", "A1.pdf"):
            document = by_name[name]
            projection = case.service.document_projection(case.matter.matter_id,
                document.document_id, document.version_id)
            assert "begin_bates" not in projection["values"]
            assert ("Generated evidence", "must remain ambiguous") not in projection["raw_fields"]
        assert case.store.source_path(source.document_id, verify_digest=True).read_bytes() == supplied


@pytest.mark.parametrize("case,expected", [
    ("text", "matched"), ("textpath", "matched"), ("second-native", "ambiguous_match"),
    ("missing-native", "unmatched"), ("bare-text-name", "ambiguous_match"),
    ("same-file", "conflicting_match"),
])
def test_dat_companion_role_disambiguates_only_the_declared_native(tmp_path, case, expected):
    from tests.test_discovery_review_dat_opt_storage import finish_import, storage_case

    text_path = "Text/A1.txt"
    native = "Natives/A1.txt"
    supplied_native = {"missing-native": "Missing/A1.txt", "same-file": text_path}.get(case, native)
    supplied_text = "A1.txt" if case == "bare-text-name" else text_path
    data = dat_bytes(("BEGBATES", "NATIVEPATH", "TEXTPATH" if case == "textpath" else "TEXT"),
                     [("A1", supplied_native, supplied_text)])
    # Explicit historical IDs put the text and native before the third stem witness.
    files = [(text_path, source_text(1))]
    if case != "missing-native":
        files.append((native, source_text(2)))
    if case == "second-native":
        files.append(("Other/A1.txt", source_text(3)))
    with storage_case(tmp_path, count=1, primary_bytes=data, extra_files=files) as current:
        attempt = finish_import(current)
        row, = current.repository.records(current.matter.matter_id, attempt["attempt_id"], category="document")
        assert row["association_status"] == expected
        if expected == "matched":
            assert current.store.get(row["document_id"]).relative_path == "Generated/" + native
        else:
            assert not row["document_id"]


def test_dat_text_role_preserves_contradictory_produced_ids(review):
    files = {"Generated/Natives/A1.txt": source_text(1), "Generated/Text/A1.txt": source_text(2),
        "Generated/legacy.csv": b"CONTROLNUMBER,NATIVEPATH\nC2,Generated/Text/A1.txt\n",
        "Generated/load.dat": dat_bytes(("BEGBATES", "CONTROLNUMBER", "NATIVEPATH", "TEXTPATH"),
            [("A1", "C2", "Natives/A1.txt", "Text/A1.txt")])}
    upload_production(review, files)
    attempts = import_paths(review, ["Generated/legacy.csv", "Generated/load.dat"])
    control, = all_records(review, attempts["Generated/legacy.csv"])
    assert control["document_id"] == review.document("Generated/Text/A1.txt").document_id
    row, = all_records(review, attempts["Generated/load.dat"])
    assert row["association_status"] == "conflicting_match" and not row["document_id"]


@pytest.mark.parametrize("headers,values,expected", [
    (("TEXT", "TEXTPATH"), ("Text/A1.txt", "Text/other.txt"), "unmatched"),
    (("TEXT",), ("A1.txt",), "unmatched"),
    (("TEXT",), ("../Text/A1.txt",), "unmatched"),
    (("TEXT", "TEXTPATH", "NATIVEPATH"), ("Text/other.txt", "Text/different.txt", "Text/A1.txt"), "matched"),
])
def test_unresolved_text_role_cannot_establish_native_by_stem(tmp_path, headers, values, expected):
    from tests.test_discovery_review_dat_opt_storage import finish_import, storage_case

    data = dat_bytes(("BEGBATES", *headers), [("A1", *values)])
    files = [("Text/A1.txt", source_text(1)), ("Text/other.txt", source_text(2))]
    with storage_case(tmp_path, count=1, primary_bytes=data, extra_files=files) as current:
        attempt = finish_import(current)
        row, = current.repository.records(current.matter.matter_id, attempt["attempt_id"], category="document")
        assert row["association_status"] == expected
        assert bool(row["document_id"]) is (expected == "matched")


@pytest.mark.parametrize("role,collision", [("NATIVEPATH", True), ("TEXT", True), ("TEXT", False)])
def test_volume_named_subfolder_requires_a_unique_exact_path(tmp_path, role, collision):
    from tests.test_discovery_review_dat_opt_storage import finish_import, storage_case

    supplied = "Generated/Documents/A1.txt"
    other = "Other/companion.txt"
    headers = ("BEGBATES", "NATIVEPATH", "TEXT")
    data = dat_bytes(headers, [("S1", supplied, other) if role == "NATIVEPATH" else ("S1", other, supplied)])
    files = [(supplied, source_text(1)), (other, source_text(2))]
    if collision:
        files.append(("Documents/A1.txt", source_text(3)))
    with storage_case(tmp_path, count=1, primary_bytes=data, extra_files=files) as current:
        attempt = finish_import(current)
        row, = current.repository.records(current.matter.matter_id, attempt["attempt_id"], category="document")
        if role == "NATIVEPATH":
            assert row["association_status"] == "ambiguous_match"
        else:
            _, status, candidate = current.repository.dat_text_companion(current.matter.matter_id, attempt, row)
            assert status == ("ambiguous" if collision else "matched")
            if candidate:
                assert candidate["relative_path"] == "Generated/" + supplied


@pytest.mark.parametrize("prior_revision", ["production-record-events-1", "production-record-events-2"])
def test_reopening_prior_matcher_rebuilds_dat_associations(tmp_path, monkeypatch, prior_revision):
    from case_intelligence.modules.discovery_review.production import service
    from tests.test_discovery_review_dat_opt_storage import finish_import, reopen_storage_case, storage_case

    with storage_case(tmp_path, count=1) as current:
        with monkeypatch.context() as previous:
            previous.setattr(service, "PARSER_REVISION", prior_revision)
            old = finish_import(current)
        reopen_storage_case(current, tmp_path)
        for _ in range(30):
            if current.service.advance(current.matter.matter_id, max_steps=128)["state"] == "complete":
                break
        new = current.repository.current_attempt(current.matter.matter_id, old["document_id"])
        assert new["status"] == "complete" and new["parser_revision"] == service.PARSER_REVISION
        assert new["attempt_id"] != old["attempt_id"] and new["digest"] == old["digest"]


def test_reopened_store_requires_new_production_validation(tmp_path):
    runtime = tmp_path / "runtime"
    with opened_review(runtime) as current:
        upload_production(current, {"Generated/native.txt": source_text(1),
            "Generated/load.dat": dat_bytes(("BEGBATES", "NATIVEPATH"), [("SYN0001", "Generated/native.txt")])})
        attempt_id = import_paths(current, ["Generated/load.dat"])["Generated/load.dat"]
        original = current.repository.attempt(current.matter.matter_id, attempt_id)
        old_token = current.service.input_revision(current.matter.matter_id)
        slug = current.matter.slug
        assert original["input_revision"] == old_token
        fresh = ProductionService(current.service.context, repository=current.repository)
        fresh.bind(current.matter, current.store, current.definition)
        assert fresh.input_revision(current.matter.matter_id) == old_token
        current.service = fresh
        assert import_paths(current, ["Generated/load.dat"])["Generated/load.dat"] == attempt_id
        same_store = current.repository.attempt(current.matter.matter_id, attempt_id)
        assert same_store["generation"] == original["generation"]
        assert current.projection("Generated/native.txt")["values"]["begin_bates"] == "SYN0001"
    with opened_review(runtime, slug) as reopened:
        # PilotStore's process-local counter resets on open. Establish a complete
        # generation at that value, then reopen again with the same counter.
        assert reopened.service.input_revision(reopened.matter.matter_id) != old_token
        attempt_id = import_paths(reopened, ["Generated/load.dat"])["Generated/load.dat"]
        prior = reopened.repository.attempt(reopened.matter.matter_id, attempt_id)
        revision = reopened.store.revision
        prior_token = reopened.service.input_revision(reopened.matter.matter_id)
        assert prior["counts"] == original["counts"]
        assert reopened.projection("Generated/native.txt")["values"]["begin_bates"] == "SYN0001"
    with opened_review(runtime, slug) as again:
        assert again.store.revision == revision
        assert again.service.input_revision(again.matter.matter_id) != prior_token
        before = again.projection("Generated/native.txt")
        assert before["state"] == "pending" and not before["values"]
        assert import_paths(again, ["Generated/load.dat"])["Generated/load.dat"] == attempt_id
        revalidated = again.repository.attempt(again.matter.matter_id, attempt_id)
        assert revalidated["generation"] != prior["generation"]
        assert revalidated["input_revision"] == again.service.input_revision(again.matter.matter_id)
        assert revalidated["counts"] == prior["counts"]
        assert again.projection("Generated/native.txt")["values"]["begin_bates"] == "SYN0001"
        assert len(tuple(all_records(again, attempt_id))) == 1


def test_authorized_source_classifier_preserves_registered_formats(tmp_path):
    path = tmp_path / "protected-original"
    path.write_bytes(dat_bytes(("BEGBATES",), [("SYN0001",)]))
    for name, expected in (("Generated/input.DAT", "dat"), ("input.OPT", "opt"),
                           ("input.CSV", "csv"), ("input.TSV", "tsv"),
                           ("input.unknown", ""), ("input", ""), ("input.dat.exe", "")):
        assert load_files.classify_source(path, name=name) == expected


def test_zero_record_failed_source_remains_visible_on_both_endpoints(tmp_path):
    from tests.test_discovery_review_dat_opt_storage import app_case, source_identity

    # Real previously retained bytes exercise parser failure; this fixture does
    # not claim that current upload admission accepts an unsupported encoding.
    original = b"\xff\xfe\x00\x00" + "BEGBATES\r\nSYN0001\r\n".encode("utf-32-le")
    with app_case(tmp_path, count=1, extra_files=(("unreadable.dat", original),)) as case:
        document = next(document for document in case.store.documents.values()
                        if document.display_name == "unreadable.dat")
        source = source_identity(document, case.repository, case.matter)
        path = case.store.source_path(document.document_id, verify_digest=True)
        failed = case.service.import_source(case.matter.matter_id, source, path, case.definition,
            target=f"{case.store.revision}:{definition_digest(case.definition)}")
        assert failed["status"] == "failed" and failed["source_format"] == "dat"
        assert failed["counts"] == dict(accepted=0, rejected=0, matched=0, unmatched=0, conflicted=0)
        assert "unsupported_encoding" in failed["file_failure"]
        token = case.store.action_token(document)
        base = f"/matters/{case.matter.slug}/production"
        for endpoint in (base, base + "/items/" + token):
            response = case.client.get(endpoint, params={"doc": token, "view": "fields"})
            assert response.status_code == 200, response.text
            assert response.headers["cache-control"] == "no-store"
            assert "unsupported_encoding" in html.unescape(response.text)
            assert "Production metadata could not be prepared" in response.text
            assert "data-record-ordinal" not in response.text
            missing = case.client.get(endpoint, params={"doc": token, "view": "fields",
                "record": f"dat:{failed['attempt_id']}:1"})
            assert missing.status_code == 404
        assert path.read_bytes() == original
        persisted = case.repository.current_attempt(case.matter.matter_id, document.document_id)
        assert persisted["status"] == "failed" and persisted["counts"] == failed["counts"]


def test_stored_get_budget_and_production_cursor_invalidation(review, monkeypatch):
    from case_intelligence.modules.discovery_review import reader
    from case_intelligence.modules.discovery_review.stored_index.models import Budget
    from tests.test_discovery_review_index import service_for

    upload_production(review, {
        'Generated/native.txt': source_text(1),
        'Generated/load.dat': dat_bytes(('BEGBATES', 'NATIVEPATH'),
                                       [('SYN0001', 'Generated/native.txt')]),
    })
    budgets = []
    original = Budget.charge

    def charged(budget, rows, *, credit=False):
        if not any(previous is budget for previous in budgets):
            budgets.append(budget)
        original(budget, rows, credit=credit)
        assert budget.touches <= 256

    def forbidden(*args, **kwargs):
        raise AssertionError('GET must use the stored summary, lookup and page APIs')

    monkeypatch.setattr(Budget, 'charge', charged)
    monkeypatch.setattr(reader, 'index_for', forbidden)
    monkeypatch.setattr(reader, 'snapshot', forbidden)
    monkeypatch.setattr(reader, 'read_load_files', forbidden)
    service = service_for(review.bench)
    responses = []
    for query in ('', 'native'):
        budgets.clear()
        response = review.client.get(review.base, params={'q': query})
        assert response.status_code == 200
        assert len(budgets) <= 128
        assert service.jobs[review.matter.matter_id].attempts <= 128
        responses.append(response)
    assert service.repository.state(review.matter.matter_id)['phase'] == 'complete'
    links = [html.unescape(re.search(r'<tr[^>]*data-url="([^"]+)"', response.text)[1])
             for response in responses]
    assert all('cursor=' in link for link in links)
    document = review.document('Generated/load.dat')
    attempt = review.repository.current_attempt(review.matter.matter_id, document.document_id)
    old_generation = attempt['generation']
    review.repository.restart_matching(review.matter.matter_id, attempt['attempt_id'], attempt['target'])
    for link in links:
        budgets.clear()
        response = review.client.get(link)
        assert response.status_code == 200
        assert 'The list changed. Navigation restarted' in response.text
        assert len(budgets) <= 128
    current = review.repository.attempt(review.matter.matter_id, attempt['attempt_id'])
    assert current['status'] == 'complete' and current['generation'] != old_generation
    stored = service.repository.full_item(review.matter.matter_id, review.document('Generated/native.txt').document_id)
    assert stored['values']['begin_bates'] == 'SYN0001'


def test_source_notifications_leave_unrelated_production_generation_intact(review):
    upload_production(review, {
        'Generated/native.txt': source_text(1),
        'Generated/load.dat': dat_bytes(('BEGBATES', 'NATIVEPATH'),
                                       [('SYN0001', 'Generated/native.txt')]),
    })
    attempts = import_paths(review, ['Generated/load.dat'])
    prior = review.repository.attempt(review.matter.matter_id, attempts['Generated/load.dat'])
    upload_production(review, {'Other/unrelated.txt': source_text(2)})
    for unused in range(100):
        if review.service.advance(review.matter.matter_id)['state'] == 'complete':
            break
    else:
        pytest.fail('Unrelated source notification did not reconcile')
    current = review.repository.attempt(review.matter.matter_id, prior['attempt_id'])
    assert current['generation'] == prior['generation']
    assert current['counts'] == prior['counts']
    assert review.projection('Generated/native.txt')['values']['begin_bates'] == 'SYN0001'



def test_long_dat_heading_remains_inspectable_after_index_publication(review):
    heading = 'Generated raw heading ' * 20
    literal = 'Preserved without a chooser column'
    upload_production(review, {
        'Generated/native.txt': source_text(1),
        'Generated/load.dat': dat_bytes(('BEGBATES', 'NATIVEPATH', heading),
            [('SYN0001', 'Generated/native.txt', literal)]),
    })
    complete_listing(review)
    page = review.client.get(review.base + '/items/' + review.token('Generated/native.txt'), params={'view': 'fields'})
    assert page.status_code == 200
    assert heading in html.unescape(page.text) and literal in html.unescape(page.text)
    assert 'Metadata pending' not in page.text and 'indexing error' not in page.text
