from __future__ import annotations

import io
import asyncio
import threading
import time
import unicodedata
import zipfile
from types import SimpleNamespace
from pathlib import Path

from fastapi.testclient import TestClient
import pytest
from pypdf import PdfWriter

from case_intelligence.pilot_uploads import (
    MAX_FILE_BYTES, PilotStore, RawUploadLimitMiddleware, UploadProblem,
)
from case_intelligence.review_bench import create_app
from case_intelligence import review_bench
from case_intelligence import pilot_uploads

ROOT = Path(__file__).parents[1]
PDF = ROOT / "src/case_intelligence/demo_data/synthetic_case_report.pdf"


def _upload(client, name: str, data: bytes, mime: str):
    return client.post("/matters/pilot/uploads", files=[("files", (name, data, mime))])


def _docx_bytes(*paragraphs: str) -> bytes:
    escaped = [
        paragraph.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        for paragraph in paragraphs
    ]
    body = "".join(
        f'<w:p><w:r><w:t xml:space="preserve">{paragraph}</w:t></w:r></w:p>'
        for paragraph in escaped
    )
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body>{body}<w:sectPr/></w:body></w:document>"
    ).encode()
    content_types = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/word/document.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        '</Types>'
    ).encode()
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("word/document.xml", document)
    return output.getvalue()


def test_docx_upload_extracts_searchable_sections(tmp_path):
    store = PilotStore(tmp_path / "pilot")
    document, _ = store.store_stream(
        "interview notes.docx",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        io.BytesIO(
            _docx_bytes(
                "OFFICER LEE INTERVIEW",
                "Officer Lee stated that the canvas bag was first visible at 10:12 p.m.",
                "The vehicle had already stopped before that observation.",
            )
        ),
    )
    assert document.state == "ready"
    assert document.message == "1 section ready and searchable"
    assert "10:12 p.m." in document.parsed_units()[0].text


def test_pilot_workspace_and_txt_upload_search_answer_exact_line_and_no_leakage(tmp_path):
    runtime = tmp_path / "runtime"
    app = create_app(runtime)
    with TestClient(app) as client:
        workspace = client.get("/matters/pilot")
        assert workspace.status_code == 200
        assert "Upload PDF or TXT" in workspace.text
        response = _upload(client, "interview notes.txt", b"First line\nThe copper lantern was placed in locker seven.\nLast line\n", "text/plain")
        assert response.status_code == 200
        assert "ready and searchable" in response.text
        search = client.get("/matters/pilot", params={"q": "copper lantern locker"})
        assert "interview notes.txt" in search.text
        assert "Lines 1–3" in search.text
        href = next(part.split('"')[0] for part in search.text.split('/matters/pilot/sources/')[1:] if "?line=1" in part)
        source = client.get("/matters/pilot/sources/" + href)
        assert source.status_code == 200
        assert 'id="line-1"' in source.text and "locker seven" in source.text
        answer = client.get("/matters/pilot", params={"question": "Where was the copper lantern placed?"})
        assert "locker seven" in answer.text
        combined = workspace.text + response.text + search.text + source.text + answer.text
        for forbidden in (str(runtime), "/home/", "matter-pilot", "sha256", "postgresql://", "ibm-granite", "vector score"):
            assert forbidden not in combined


def test_pdf_upload_page_navigation_and_persistence_restart(tmp_path):
    runtime = tmp_path / "runtime"
    with TestClient(create_app(runtime)) as client:
        response = _upload(client, "evidence.pdf", PDF.read_bytes(), "application/pdf")
        assert "3 of 3 pages with searchable text" in response.text
        assert "Complete page reading is not established" in response.text
        search = client.get("/matters/pilot", params={"q": "blue canvas bag"})
        assert "evidence.pdf · Page 3" in search.text
        document_id = search.text.split("/matters/pilot/sources/", 1)[1].split("?page=3", 1)[0]
    with TestClient(create_app(runtime)) as restarted:
        workspace = restarted.get("/matters/pilot")
        assert "evidence.pdf" in workspace.text
        source = restarted.get(f"/matters/pilot/sources/{document_id}?page=3")
        assert "Page 3 of 3" in source.text and "blue canvas bag" in source.text.casefold()


def test_scanned_empty_pdf_shows_needs_ocr(tmp_path):
    stream = io.BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    writer.write(stream)
    with TestClient(create_app(tmp_path / "runtime")) as client:
        response = _upload(client, "scan.pdf", stream.getvalue(), "application/pdf")
        assert "Needs OCR" in response.text
        search = client.get("/matters/pilot", params={"q": "anything"})
        assert "No matching record found" in search.text


def test_selective_cpu_ocr_recovers_blank_page_and_preserves_page_citation(tmp_path, monkeypatch):
    stream = io.BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    writer.write(stream)
    monkeypatch.setenv("CASE_REVIEW_OCR_MODE", "selective")
    monkeypatch.setattr(
        pilot_uploads,
        "_ocr_pdf_page",
        lambda source, page: pilot_uploads.PdfOcrResult("The violet folder was scanned on page one.", "recognized"),
    )
    with TestClient(create_app(tmp_path / "runtime")) as client:
        response = _upload(client, "scan.pdf", stream.getvalue(), "application/pdf")
        assert "OCR returned text: 1" in response.text
        search = client.get("/matters/pilot", params={"q": "violet folder scanned"})
        assert "scan.pdf · Page 1" in search.text
        path = "/matters/pilot/sources/" + search.text.split("/matters/pilot/sources/", 1)[1].split('"', 1)[0]
        source = client.get(path)
        assert "violet folder" in source.text.casefold()


def test_selective_ocr_attempts_at_most_25_blank_pages(tmp_path, monkeypatch):
    attempts = []
    monkeypatch.setenv("CASE_REVIEW_OCR_MODE", "selective")
    monkeypatch.setattr(
        "case_intelligence.review_bench_v2.extract_pdf_pages",
        lambda source: tuple(SimpleNamespace(page_number=n, text="") for n in range(1, 31)),
    )
    monkeypatch.setattr(
        pilot_uploads,
        "_ocr_pdf_page",
        lambda source, page: attempts.append(page) or pilot_uploads.PdfOcrResult(status="no_text"),
    )
    store = PilotStore(tmp_path / "pilot")
    document, _ = store.store_stream(
        "scan.pdf", "application/pdf", io.BytesIO(b"%PDF-1.7\nsynthetic")
    )
    assert document.state == "needs_ocr"
    assert attempts == list(range(1, 26))


def test_concurrent_same_name_is_serialized_and_persists_once(tmp_path, monkeypatch):
    store = PilotStore(tmp_path / "pilot")
    original = store._extract
    counter_lock = threading.Lock()
    active = 0
    maximum = 0

    def slow_extract(document, path=None):
        nonlocal active, maximum
        with counter_lock:
            active += 1
            maximum = max(maximum, active)
        time.sleep(0.05)
        try:
            return original(document, path)
        finally:
            with counter_lock:
                active -= 1

    monkeypatch.setattr(store, "_extract", slow_extract)
    barrier = threading.Barrier(3)
    results = []
    errors = []

    def upload():
        barrier.wait()
        try:
            results.append(store.store_stream("same.txt", "text/plain", io.BytesIO(b"same bytes"))[0])
        except Exception as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    threads = [threading.Thread(target=upload) for _ in range(2)]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join()
    assert errors == []
    assert maximum == 1
    assert len({item.document_id for item in results}) == 1
    assert len(store.documents) == 1
    assert len(list(store.files.iterdir())) == 1
    assert len(PilotStore(store.root).documents) == 1


def test_restart_fails_closed_when_stored_bytes_change(tmp_path):
    store = PilotStore(tmp_path / "pilot")
    document, _ = store.store_stream("source.txt", "text/plain", io.BytesIO(b"original citation"))
    (store.files / document.stored_name).write_bytes(b"changed bytes now")
    restarted = PilotStore(store.root)
    changed = restarted.get(document.document_id)
    assert changed.state == "changed"
    assert changed.units == []
    assert tuple(restarted.ready_documents()) == ()
    with pytest.raises(UploadProblem, match="cannot be retried") as refused:
        restarted.retry(document.document_id)
    assert refused.value.status_code == 409
    assert restarted.get(document.document_id).digest == document.digest
    assert restarted.get(document.document_id).version_id == document.version_id


def test_changed_bytes_have_no_retry_action_and_retry_route_returns_409(tmp_path):
    runtime = tmp_path / "runtime"
    with TestClient(create_app(runtime)) as client:
        _upload(client, "source.txt", b"original citation", "text/plain")
        document = next(iter(client.app.state.bench.pilot.documents.values()))
        token = client.app.state.bench.pilot.action_token(document)
        (client.app.state.bench.pilot.files / document.stored_name).write_bytes(b"changed bytes now")
    with TestClient(create_app(runtime)) as restarted:
        workspace = restarted.get("/matters/pilot")
        assert "Stored file changed after processing" in workspace.text
        assert f'action="/matters/pilot/sources/{token}/retry"' not in workspace.text
        response = restarted.post(f"/matters/pilot/sources/{token}/retry")
        assert response.status_code == 409
        assert "cannot be retried" in response.text
        current = restarted.app.state.bench.pilot.get(document.document_id)
        assert current.state == "changed" and current.units == []


def test_mixed_pdf_viewer_preserves_original_page_total(tmp_path, monkeypatch):
    with TestClient(create_app(tmp_path / "runtime")) as client:
        monkeypatch.setattr(
            "case_intelligence.review_bench_v2.extract_pdf_pages",
            lambda source: (
                SimpleNamespace(page_number=1, text="First searchable page."),
                SimpleNamespace(page_number=2, text=""),
                SimpleNamespace(page_number=3, text="The amber notebook is on page three."),
            ),
        )
        response = _upload(client, "mixed.pdf", b"%PDF-1.7\nsynthetic", "application/pdf")
        assert "2 of 3 pages with searchable text" in response.text
        search = client.get("/matters/pilot", params={"q": "amber notebook page three"})
        path = "/matters/pilot/sources/" + search.text.split("/matters/pilot/sources/", 1)[1].split('"', 1)[0]
        source = client.get(path)
        assert "Page 3 of 3" in source.text


def test_store_rejects_paths_mime_malformed_empty_oversize_and_symlinks(tmp_path):
    store = PilotStore(tmp_path / "pilot")
    for name in ("../escape.txt", "folder/file.txt", "folder\\file.txt", ".."):
        with pytest.raises(UploadProblem):
            store.store_stream(name, "text/plain", io.BytesIO(b"ok"))
    with pytest.raises(UploadProblem, match="match"):
        store.store_stream("fake.pdf", "text/plain", io.BytesIO(b"%PDF-1.4"))
    with pytest.raises(UploadProblem, match="valid PDF"):
        store.store_stream("fake.pdf", "application/pdf", io.BytesIO(b"not pdf"))
    with pytest.raises(UploadProblem, match="Empty"):
        store.store_stream("empty.txt", "text/plain", io.BytesIO())
    with pytest.raises(UploadProblem, match="25 MiB"):
        store.store_stream("large.txt", "text/plain", io.BytesIO(b"x" * (MAX_FILE_BYTES + 1)))
    linked = tmp_path / "linked"
    linked.symlink_to(tmp_path / "pilot", target_is_directory=True)
    with pytest.raises(RuntimeError, match="symbolic"):
        PilotStore(linked)


def test_duplicate_contract_and_remove_is_bounded(tmp_path):
    store = PilotStore(tmp_path / "pilot")
    first, used = store.store_stream("same.txt", "text/plain", io.BytesIO(b"alpha"))
    retry, _ = store.store_stream("SAME.TXT", "text/plain", io.BytesIO(b"alpha"), request_used=used)
    assert retry.document_id == first.document_id
    with pytest.raises(UploadProblem, match="Rename") as conflict:
        store.store_stream("same.txt", "text/plain", io.BytesIO(b"bravo"), request_used=used)
    assert conflict.value.status_code == 409
    second, _ = store.store_stream("other.txt", "text/plain", io.BytesIO(b"alpha"), request_used=used)
    assert first.document_id != second.document_id
    assert (store.files / first.stored_name).read_bytes() == b"alpha"
    store.remove(first.document_id)
    assert second.document_id in store.documents


@pytest.mark.parametrize("name", [
    "../../x.pdf", "/tmp/x.pdf", "C:\\x.pdf", "\\\\server\\share\\x.pdf",
    "a/b.pdf", "a\\b.pdf", "x:y.pdf", "CON.txt", "LPT1.pdf",
    "report.pdf.", "report.pdf ", "bad\x00.txt", "bad\x01.txt", "bad\u202e.txt",
    "", "   ", "x" * 241 + ".txt",
])
def test_display_name_rejections_leave_no_residue(tmp_path, name):
    store = PilotStore(tmp_path / "pilot")
    with pytest.raises(UploadProblem):
        store.store_stream(name, "application/pdf" if name.casefold().endswith("pdf") else "text/plain", io.BytesIO(b"safe"))
    assert not store.documents
    assert list(store.staging.iterdir()) == []
    assert list(store.files.iterdir()) == []


def test_display_name_nfc_and_windows_equivalent_collision(tmp_path):
    store = PilotStore(tmp_path / "pilot")
    decomposed = "cafe\u0301.txt"
    first, _ = store.store_stream(decomposed, "text/plain", io.BytesIO(b"same"))
    assert first.display_name == unicodedata.normalize("NFC", decomposed)
    retry, _ = store.store_stream("CAFÉ.TXT", "text/plain", io.BytesIO(b"same"))
    assert retry.document_id == first.document_id


def _run_raw_middleware(headers, messages, limit=8):
    called = 0
    sent = []

    async def downstream(scope, receive, send):
        nonlocal called
        called += 1
        while (await receive()).get("more_body", False):
            pass
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    queue = list(messages)

    async def receive():
        return queue.pop(0)

    async def send(message):
        sent.append(message)

    scope = {"type": "http", "method": "POST", "path": "/matters/pilot/uploads", "headers": headers}
    asyncio.run(RawUploadLimitMiddleware(downstream, limit=limit)(scope, receive, send))
    return called, sent[0]["status"]


def test_raw_asgi_limit_counts_chunked_no_header_and_lying_header_before_route():
    chunks = [
        {"type": "http.request", "body": b"1234", "more_body": True},
        {"type": "http.request", "body": b"56789", "more_body": False},
    ]
    assert _run_raw_middleware([], chunks) == (0, 413)
    assert _run_raw_middleware([(b"content-length", b"2")], chunks) == (0, 413)


@pytest.mark.parametrize("headers", [
    [(b"content-length", b"-1")],
    [(b"content-length", b"nope")],
    [(b"content-length", b"9")],
    [(b"content-length", b"2"), (b"content-length", b"3")],
])
def test_raw_asgi_limit_rejects_bad_or_conflicting_length_before_route(headers):
    messages = [{"type": "http.request", "body": b"x", "more_body": False}]
    assert _run_raw_middleware(headers, messages) == (0, 413)


def test_txt_bounds_bom_nul_empty_and_exact_inclusive_lines(tmp_path):
    store = PilotStore(tmp_path / "pilot")
    document, _ = store.store_stream("notes.txt", "text/plain", io.BytesIO(b"\xef\xbb\xbfA\r\nB\r\nC\n"))
    unit = document.parsed_units()[0]
    assert (unit.line_start, unit.line_end, unit.text) == (1, 3, "A\nB\nC")
    for name, payload in (("nul.txt", b"a\x00b"), ("blank.txt", b" \n\t\n"), ("utf16.txt", b"\xff\xfeA\x00")):
        with pytest.raises(UploadProblem):
            store.store_stream(name, "text/plain", io.BytesIO(payload))
    assert list(store.staging.iterdir()) == []


def test_orphan_staging_reconciled_on_restart_without_following_link(tmp_path):
    root = tmp_path / "pilot"
    store = PilotStore(root)
    orphan = store.staging / (".upload-" + "a" * 32 + ".part")
    target = tmp_path / "target"
    target.write_text("keep", encoding="utf-8")
    orphan.symlink_to(target)
    restarted = PilotStore(root)
    assert target.read_text(encoding="utf-8") == "keep"
    assert list(restarted.staging.iterdir()) == []


def test_http_duplicate_semantics_and_internal_values_never_render(tmp_path):
    with TestClient(create_app(tmp_path / "runtime")) as client:
        first = _upload(client, "Report.TXT", b"private searchable words", "text/plain")
        assert first.status_code == 200
        document = next(iter(client.app.state.bench.pilot.documents.values()))
        retry = _upload(client, "report.txt", b"private searchable words", "text/plain")
        assert retry.status_code == 200
        assert len(client.app.state.bench.pilot.documents) == 1
        conflict = _upload(client, "REPORT.txt", b"different bytes", "text/plain")
        assert conflict.status_code == 409
        assert "Rename this file" in conflict.text
        rendered = client.get("/matters/pilot", params={"q": "private searchable"}).text
        assert document.digest not in rendered
        assert document.version_id not in rendered
        assert document.stored_name not in rendered
        assert list(client.app.state.bench.pilot.staging.iterdir()) == []


def test_encrypted_and_malformed_pdf_fail_plainly_without_residue(tmp_path):
    encrypted = io.BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    writer.encrypt("")
    writer.write(encrypted)
    with TestClient(create_app(tmp_path / "runtime")) as client:
        response = _upload(client, "encrypted.pdf", encrypted.getvalue(), "application/pdf")
        assert response.status_code == 400
        assert "Encrypted PDFs" in response.text
        malformed = _upload(client, "broken.pdf", b"%PDF-1.7\nnot a real xref", "application/pdf")
        assert malformed.status_code == 400
        assert "damaged or malformed" in malformed.text
        assert not client.app.state.bench.pilot.documents
        assert list(client.app.state.bench.pilot.staging.iterdir()) == []


def test_pdf_timeout_is_plain_and_cleans_staging(tmp_path, monkeypatch):
    import case_intelligence.review_bench_v2 as module

    def timeout(*args, **kwargs):
        raise module.subprocess.TimeoutExpired(args[0], 15)

    store = PilotStore(tmp_path / "pilot")
    monkeypatch.setattr(module.subprocess, "run", timeout)
    with pytest.raises(UploadProblem, match="timed out"):
        store.store_stream("slow.pdf", "application/pdf", io.BytesIO(b"%PDF-1.7\n"))
    assert not store.documents
    assert list(store.staging.iterdir()) == []
    assert list(store.files.iterdir()) == []


@pytest.mark.parametrize(
    ("constant", "value", "payload", "message"),
    [
        ("MAX_TEXT_CHARS", 3, b"four", "characters"),
        ("MAX_TEXT_LINES", 2, b"a\nb\nc", "lines"),
        ("MAX_TEXT_LINE_CHARS", 3, b"four", "line that is too long"),
        ("MAX_TEXT_CHUNKS", 1, b"a\n\n\n\n\n\n\n\n\n\n\n\n\n\n\n\n\n\n\n\na", "sections"),
    ],
)
def test_txt_resource_limits_fail_without_residue(tmp_path, monkeypatch, constant, value, payload, message):
    import case_intelligence.pilot_uploads as module

    monkeypatch.setattr(module, constant, value)
    store = PilotStore(tmp_path / "pilot")
    with pytest.raises(UploadProblem, match=message):
        store.store_stream("bounded.txt", "text/plain", io.BytesIO(payload))
    assert not store.documents
    assert list(store.staging.iterdir()) == []
    assert list(store.files.iterdir()) == []


def test_source_click_revalidates_version_and_excerpt_digest(tmp_path):
    app = create_app(tmp_path / "runtime")
    with TestClient(app) as client:
        _upload(client, "lines.txt", b"one\ntwo target\nthree\n", "text/plain")
        search = client.get("/matters/pilot", params={"q": "two target"})
        path = "/matters/pilot/sources/" + search.text.split("/matters/pilot/sources/", 1)[1].split('"', 1)[0]
        assert client.get(path).status_code == 200
        document = next(iter(client.app.state.bench.pilot.documents.values()))
        document.version_id = "changed-version"
        stale = client.get(path)
        assert stale.status_code == 404
        assert "changed-version" not in stale.text
        assert document.digest not in stale.text


def test_upload_count_limit_matter_isolation_and_unsupported_answer_abstains(tmp_path):
    with TestClient(create_app(tmp_path / "runtime")) as client:
        files = [("files", (f"{n}.txt", b"safe", "text/plain")) for n in range(11)]
        response = client.post("/matters/pilot/uploads", files=files)
        assert "between 1 and 10" in response.text
        _upload(client, "pilot-only.txt", b"ORCHID PILOT CANARY\n", "text/plain")
        assert "ORCHID PILOT CANARY" in client.get("/matters/pilot", params={"q": "ORCHID PILOT CANARY"}).text
        alpha = client.get("/matters/alpha", params={"q": "ORCHID PILOT CANARY"}).text
        assert "No matching record found" in alpha
        answer = client.get("/matters/pilot", params={"question": "What was the blood alcohol level?"})
        assert "could not find support" in answer.text


def test_failed_only_source_blocks_question_instead_of_claiming_no_support(tmp_path):
    app = create_app(tmp_path / "runtime")
    with TestClient(app) as client:
        _upload(client, "large-role-manual.txt", b"Invented role duties.\n", "text/plain")
        document = next(iter(client.app.state.bench.pilot.documents.values()))
        document.state = "failed"
        document.message = "Saved, but indexing did not finish. Choose Try again."
        client.app.state.bench.pilot._save()
        client.app.state.bench._refresh_pilot_retrieval(index_postgres=False)

        response = client.get(
            "/matters/pilot",
            params={"question": "Describe the duties of the supervisory systems coordinator"},
        )
        assert "No uploaded source is ready to search" in response.text
        assert "could not find support" not in response.text


def test_retrieval_exception_is_not_reported_as_unsupported_and_health_stays_ready(tmp_path):
    app = create_app(tmp_path / "runtime")
    with TestClient(app) as client:
        _upload(client, "role.txt", b"Invented supervisory duties.\n", "text/plain")
        bench = client.app.state.bench
        bench.models_ready = True

        class BrokenRetriever:
            def search(self, *_args, **_kwargs):
                raise RuntimeError("synthetic retrieval outage")

        bench.hybrid = BrokenRetriever()
        response = client.get(
            "/matters/pilot",
            params={"question": "Describe the supervisory duties"},
        )
        assert "Search could not run" in response.text
        assert "could not find support" not in response.text
        assert client.get("/health").json()["capabilities"]["model_assistance"] == "ready"


def test_invalid_utf8_and_unsupported_extension_are_plain_errors(tmp_path):
    with TestClient(create_app(tmp_path / "runtime")) as client:
        bad = _upload(client, "bad.txt", b"\xff\xfe", "text/plain")
        assert "valid UTF-8" in bad.text
        malformed_docx = _upload(client, "notes.docx", b"PK\x03\x04", "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
        assert "damaged or malformed" in malformed_docx.text


def test_non_synthetic_pilot_matter_does_not_disable_postgres_model_startup(tmp_path, monkeypatch):
    captured: dict[str, object] = {}

    class FakeConnection:
        def close(self):
            captured["closed"] = True

    def fake_initialize(connection, pages_by_matter, *, embedder=None):
        captured["matter_ids"] = set(pages_by_matter)
        captured["embedder"] = embedder

    def connect_synthetic():
        return FakeConnection()

    monkeypatch.setattr(review_bench, "connect_postgres_from_environment", connect_synthetic)
    monkeypatch.setattr(review_bench, "initialize_postgres_demo", fake_initialize)
    monkeypatch.setattr(review_bench, "delete_postgres_matter_documents", lambda *args, **kwargs: None)
    monkeypatch.setenv("CASE_REVIEW_ENABLE_MODELS", "1")
    monkeypatch.setenv("CASE_REVIEW_MODEL_WORKER_URL", "http://127.0.0.1:9")

    app = create_app(tmp_path / "runtime")
    assert captured["matter_ids"] == {"matter-alpha", "matter-bravo"}
    assert app.state.bench.postgres_ready is True
    assert app.state.bench.models_ready is True
    app.state.bench.close()
