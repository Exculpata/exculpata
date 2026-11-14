"""Generated DAT/OPT boundary proofs through protected storage and real receipts."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import html
import io
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import threading
import time
import uuid

import pytest
from fastapi.testclient import TestClient

from case_intelligence.generation import UnavailableGenerator
from case_intelligence.intake_receipts import IntakeReceipts
from case_intelligence.managed_storage import StoragePolicy
from case_intelligence.malware_scan import MalwareScanResult, MalwareScannerStatus
from case_intelligence.pilot_uploads import PilotStore, UploadProblem
from case_intelligence.source_admission import (
    DIAGNOSTICS, LOAD_FILE_COMPLETION_MESSAGE, LOAD_FILE_COVERAGE_MESSAGE,
    LOAD_FILE_INTAKE_TEXT, LOAD_FILE_REJECTION_PREFIX, LOAD_FILE_SUPPORT_TEXT,
    LoadFileAdmissionError, display_load_file_reason, validate_load_file,
)
from case_intelligence.workbench import create_workbench_app
from case_intelligence.workspace_store import WorkspaceStore

ROOT = Path(__file__).resolve().parents[1]
ACTOR = "development-taylor-morgan"
SUPPORT_TEXT = (
    "DAT support requires at least two nonempty, distinct column headers and one data record, "
    "U+0014 field separators, optional thorn (þ) quoting, and the same number of fields in every "
    "record. OPT support requires no header, exactly seven comma-separated fields per record, "
    "and at least one nonblank record; quoted commas are not supported. Supported encodings "
    "are UTF-8 with an optional BOM, UTF-16 LE or BE with a BOM, and Windows-1252 detected "
    "from the first 64 KiB; later decoding errors are rejected. Each record must fit the "
    "1 MiB record limit."
)
DAT = "þControlþ\x14þNoteþ\r\nþSYN-0001þ\x14þGenerated ® noteþ\r\n".encode()
OPT = b"SYN-0001,VOL,images/one.tif,Y,,,1\r\n"


@pytest.fixture(autouse=True)
def load_file_upload_storage(monkeypatch):
    monkeypatch.setenv("CASE_INTELLIGENCE_STORAGE_RESERVE_GIB", "0")


class CountingScanner:
    """Supply explicit synthetic scanner outcomes without replacing admission."""

    def __init__(self, outcome="clean", ready=True):
        self.outcome = outcome
        self.ready = ready
        self.calls = []

    def status(self, *, force=False):
        return MalwareScannerStatus("ready" if self.ready else "unavailable", "synthetic")

    def scan(self, path):
        self.calls.append(path.read_bytes())
        return MalwareScanResult(self.outcome, "synthetic", message="Generated scan outcome")


@contextmanager
def pilot(root, *, scanner=None, mode="extended", limit=25 * 1024 * 1024):
    store = PilotStore(root, malware_scanner=scanner or CountingScanner(),
                       malware_scan_mode=mode, document_file_limit=limit)
    try:
        yield store
    finally:
        store.close()


def put_resumable(store, body):
    item_id = "upload-item-" + uuid.uuid4().hex
    for offset in range(0, len(body), 1024 * 1024):
        chunk = body[offset:offset + 1024 * 1024]
        assert store.append_resumable_chunk(item_id, offset=offset,
            expected_size=len(body), chunk=chunk) == offset + len(chunk)
    return item_id


def admit(store, name, body, *, resumable=False, deferred=False, relative_path=None,
          media_type="text/plain"):
    relative_path = relative_path or name
    if resumable:
        item_id = put_resumable(store, body)
        document = store.finalize_resumable_upload(item_id, display_name=name,
            relative_path=relative_path, content_type=media_type, expected_size=len(body))
        store.discard_resumable_upload(item_id)
        assert document.state == "queued"
        return document if deferred else store.process(document.document_id)
    document, used = store.store_stream(name, media_type, io.BytesIO(body),
        relative_path=relative_path, defer_processing=deferred)
    assert used == len(body)
    return document


def exact_original(store, document, body):
    path = store.files / document.stored_name
    assert path.read_bytes() == body
    assert document.digest == hashlib.sha256(body).hexdigest()
    assert document.size == len(body)
    assert document.media_type == "text/plain"
    assert re.fullmatch(r"[0-9a-f]{32}", document.stored_name)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert document.units == [] and document.units_file == ""
    assert document.page_count == document.completed_units == document.total_units == 0


def app_at(runtime, scanner=None, *, background=False, policy=None):
    return create_workbench_app(runtime, generator=UnavailableGenerator(), auth_mode="test",
        background_ingestion=background, ingestion_workers=1,
        malware_scanner=scanner or CountingScanner(), malware_scan_mode="extended",
        storage_policy=policy or StoragePolicy(reserve_bytes=0))


def new_matter(client):
    response = client.post("/matters", data={"name": "Generated load-file review"},
                           follow_redirects=False)
    assert response.status_code == 303, response.text
    slug = response.headers["location"].split("/")[2]
    bench = client.app.state.workbench
    return bench, bench.matter(slug, ACTOR)


def descriptor(path, size, media_type="text/plain"):
    return {"name": path.rsplit("/", 1)[-1], "relative_path": path,
            "size": size, "media_type": media_type}


def receipt_upload(client, matter, name, body):
    root = f"/matters/{matter.slug}/intake-receipts"
    files = [descriptor(name, len(body))]
    response = client.post(root, json={"selection_key": uuid.uuid4().hex,
        "selection_fingerprint": hashlib.sha256(body).hexdigest(), "selected_count": 1,
        "eligible_indexes": [0], "collection_name": "Generated load-file selection"})
    assert response.status_code == 201, response.text
    receipt = response.json()
    receipt_root = root + "/" + receipt["receipt_id"]
    response = client.post(receipt_root + "/items", json={"start": 0,
        "files": files, "reviewed_states": ["valid"]})
    assert response.status_code == 200, response.text
    response = client.post(receipt_root + "/seal")
    assert response.status_code == 200, response.text
    receipt = response.json()
    response = client.post(f"/matters/{matter.slug}/upload-sessions", json={
        "files": files, "collection_name": "Generated load-file selection",
        "intake_receipt_id": receipt["receipt_id"], "intake_ordinals": [0]})
    assert response.status_code in {200, 201}, response.text
    session = response.json()
    item = session["items"][0]
    response = client.put(item["chunk_url"], content=body,
        headers={"Content-Type": "application/octet-stream", "X-Upload-Offset": "0"})
    assert response.status_code == 200, response.text
    return receipt, session, item


def deny_content(client, matter, document):
    token = PilotStore.action_token(document)
    url = f"/matters/{matter.slug}/sources/{token}/content"
    original = client.app.state.workbench.source_store(matter).files / document.stored_name
    with forbid_path_io(original):
        for method, headers in (("GET", {}), ("HEAD", {}), ("GET", {"Range": "bytes=0-7"}),
                                ("HEAD", {"Range": "bytes=0-7"})):
            response = client.request(method, url, headers=headers)
            assert response.status_code == 404, (method, response.status_code)
            assert "content-range" not in response.headers
            assert "content-disposition" not in response.headers


def wait_completed(bench, matter, document_ids, *, expected="succeeded"):
    deadline = time.monotonic() + 12
    while time.monotonic() < deadline:
        jobs = [bench.workspace.ingest_job(matter.matter_id, ident) for ident in document_ids]
        if all(job is not None and job.state in {"succeeded", "failed"} for job in jobs):
            assert all(job.state == expected for job in jobs), jobs
            return
        time.sleep(0.01)
    pytest.fail("Real ingestion did not reach a terminal state for the generated inputs")


def test_dat_upload_preserves_original_bytes(tmp_path):
    scanner = CountingScanner()
    with pilot(tmp_path / "sources", scanner=scanner) as store:
        document = admit(store, "arbitrary-metadata.dat", DAT)
        assert document.state == "ready"
        assert document.message == LOAD_FILE_COMPLETION_MESSAGE
        exact_original(store, document, DAT)
        assert scanner.calls == [DAT]
        assert document.document_id != document.version_id
        assert document.relative_path == "arbitrary-metadata.dat"


def test_opt_upload_preserves_original_bytes(tmp_path):
    scanner = CountingScanner()
    with pilot(tmp_path / "sources", scanner=scanner) as store:
        document = admit(store, "generated.OPT", OPT)
        exact_original(store, document, OPT)
        assert scanner.calls == [OPT]
        for index, invalid in enumerate((b"\n", b",,,,,,\n", b"one,two\n", b"a,b,c,d,e,f,g,h\n")):
            with pytest.raises(UploadProblem):
                admit(store, f"invalid-{index}.opt", invalid)
        assert len(store.documents) == 1
        assert len(list(store.files.iterdir())) == 1
        assert not list(store.staging.iterdir())


@pytest.mark.parametrize("resumable", [False, True], ids=["direct", "resumable"])
@pytest.mark.parametrize("deferred", [False, True], ids=["immediate", "deferred"])
@pytest.mark.parametrize("name,body", [("one.dat", DAT), ("two.opt", OPT)])
def test_load_file_admission_matches_both_upload_paths(tmp_path, resumable, deferred, name, body):
    scanner = CountingScanner()
    with pilot(tmp_path / "sources", scanner=scanner) as store:
        document = admit(store, name, body, resumable=resumable, deferred=deferred,
                         relative_path="Generated/" + name)
        assert document.state == ("queued" if deferred else "ready")
        exact_original(store, document, body)
        if deferred:
            document = store.process(document.document_id)
            assert document.state == "ready"
            exact_original(store, document, body)
        assert scanner.calls == [body]
        with pytest.raises(UploadProblem):
            admit(store, "bad-" + name, body + b"wrong-width\n", resumable=resumable,
                  deferred=deferred)
        assert len(store.documents) == len(list(store.files.iterdir())) == 1


@pytest.mark.parametrize("suffix", ["dat", "opt"])
def test_load_file_encodings_are_checked_to_eof(tmp_path, suffix):
    text = ("Control\x14Note\r\nSYN-1\x14Generated café ®\r\n" if suffix == "dat" else
            "SYN-1,VOL,images/café.tif,Y,,,1\r\n")
    variants = [text.encode("utf-8"), text.encode("utf-8-sig"), text.encode("cp1252"),
                b"\xff\xfe" + text.encode("utf-16-le"), b"\xfe\xff" + text.encode("utf-16-be")]
    with pilot(tmp_path / "sources") as store:
        for index, body in enumerate(variants):
            document = admit(store, f"encoding-{index}.{suffix}", body)
            exact_original(store, document, body)
        row = b"SYN-1\x14plain\n" if suffix == "dat" else b"SYN-1,VOL,one.tif,Y,,,1\n"
        prefix = (b"Control\x14Note\n" if suffix == "dat" else b"") + row * 7000
        assert len(prefix) > 65536
        invalid = [text.encode("utf-32"), text.encode("utf-32-be"),
                   prefix + row.replace(b"SYN-1", b"\xff"),
                   prefix + row.replace(b"SYN-1", b"\x81"),
                   b"\xff\xfe" + text.encode("utf-16-le") + b"\x00",
                   variants[2] + b"\x81\n"]
        for index, body in enumerate(invalid):
            with pytest.raises(UploadProblem):
                admit(store, f"damaged-{index}.{suffix}", body, deferred=True)
        assert len(store.documents) == len(variants)
        assert not list(store.staging.iterdir())


@pytest.mark.parametrize("suffix", ["dat", "opt"])
def test_load_file_content_class_rejects_disguises(tmp_path, suffix):
    valid = DAT if suffix == "dat" else OPT
    malformed_dat = [b"A\x14B\n\xc3\xbeunclosed\x14value\n", b"A\x14A\nx\x14y\n",
                     b"A\x14\nx\x14y\n", b"Only\nvalue\n", b"A\x14B\n"]
    disguises = [b"MZ" + valid, b"\x7fELF" + valid, b"PK\x03\x04" + valid,
                 b"<html><script>generated()</script></html>\n", b"ordinary undelimited text\n",
                 valid + b"\x00", valid + b"\x01", valid + b"\x1b"]
    if suffix == "dat":
        disguises.extend(malformed_dat)
    with pilot(tmp_path / "sources") as store:
        for index, body in enumerate(disguises):
            with pytest.raises(UploadProblem) as rejected:
                admit(store, f"disguised-{index}.{suffix}", body, deferred=True)
            assert LOAD_FILE_REJECTION_PREFIX in str(rejected.value)
            assert SUPPORT_TEXT in str(rejected.value)
            assert str(tmp_path) not in str(rejected.value)
        assert not store.documents and not list(store.files.iterdir())
        assert not list(store.staging.iterdir())
        hostile = ("Control\x14Note\nSYN-1\x14<script>generated()</script>\n".encode()
                   if suffix == "dat" else b"SYN-1,VOL,images/generated.tif,Y,,,1\n")
        document = admit(store, "inert-values." + suffix, hostile)
        exact_original(store, document, hostile)


def test_load_file_transport_mime_is_not_content_proof(tmp_path):
    with pilot(tmp_path / "sources") as store:
        for index, mime in enumerate((None, "", "text/plain", "text/plain; charset=windows-1252",
                                      "application/octet-stream")):
            document = admit(store, f"alias-{index}.dat", DAT, media_type=mime)
            exact_original(store, document, DAT)
        for index, mime in enumerate(("text/html", "application/x-executable", "application/zip")):
            with pytest.raises(UploadProblem, match="type does not match"):
                admit(store, f"declared-{index}.dat", DAT, media_type=mime)
        with pytest.raises(UploadProblem):
            admit(store, "canonical.opt", b"PK\x03\x04not a load file", resumable=True)
        assert len(store.documents) == 5
    with TestClient(app_at(tmp_path / "runtime", background=True)) as client:
        bench, matter = new_matter(client)
        response = client.post(f"/matters/{matter.slug}/upload-sessions", json={"files": [
            descriptor("canonical.DAT", len(DAT), "application/zip")]})
        assert response.status_code in {200, 201}, response.text
        item = response.json()["items"][0]
        assert item["media_type"] == "text/plain"
        assert not bench.source_store(matter).documents


@pytest.mark.parametrize("resumable", [False, True])
def test_load_file_limits_reject_whole_input(tmp_path, resumable):
    body = b"A\x14B\n" + b"x\x14y\n" * 10000
    with pilot(tmp_path / "exact", limit=len(body)) as store:
        document = admit(store, "exact.dat", body, resumable=resumable)
        exact_original(store, document, body)
        with pytest.raises(UploadProblem) as over:
            admit(store, "over.dat", body + b"\n", resumable=resumable)
        assert over.value.status_code == 413
        assert len(store.documents) == len(list(store.files.iterdir())) == 1
    with pilot(tmp_path / "records", limit=2 * 1024 * 1024) as store:
        for index, invalid in enumerate((b"A\x14B\n" + b"x" * (1024 * 1024 + 1) + b"\x14y\n",
                                         body + b"x\x14y\x14unexpected\n")):
            with pytest.raises(UploadProblem):
                admit(store, f"rejected-{index}.dat", invalid, resumable=resumable, deferred=True)
        assert not store.documents and not list(store.files.iterdir())
        assert not list(store.staging.iterdir())


def test_load_file_names_keep_existing_path_boundary(tmp_path):
    with pilot(tmp_path / "sources") as store:
        document = admit(store, "safe.DAT", DAT, relative_path="Generated/Nested/safe.DAT")
        assert document.relative_path == "Generated/Nested/safe.DAT"
        exact_original(store, document, DAT)
        bad_paths = ["../escape.dat", "/absolute.dat", "C:/drive.dat", "folder\\slash.dat",
                     "folder/../escape.dat", "folder/CON.dat", "folder/control\x01.dat",
                     "folder/reversed\u202e.dat"]
        for bad in bad_paths:
            with pytest.raises(UploadProblem):
                store.validate_relative_upload_path(bad)
        for bad in ("../escape.dat", "CON.dat", "control\x00.dat", "reverse\u202e.dat"):
            with pytest.raises(UploadProblem):
                store.validate_name(bad)
        assert len(store.documents) == 1


@pytest.mark.parametrize("resumable", [False, True])
def test_load_file_scan_policy_and_quarantine(tmp_path, monkeypatch, resumable):
    for mode in ("extended", "all", "disabled"):
        scanner = CountingScanner()
        with pilot(tmp_path / mode, scanner=scanner, mode=mode) as store:
            document = admit(store, "scanned.dat", DAT, resumable=resumable)
            exact_original(store, document, DAT)
            assert scanner.calls == ([] if mode == "disabled" else [DAT])
            assert "scanned" not in document.message.casefold()
    for outcome in ("unavailable", "error", "indeterminate", "infected"):
        scanner = CountingScanner(outcome, ready=outcome != "unavailable")
        with pilot(tmp_path / outcome, scanner=scanner) as store:
            with pytest.raises(UploadProblem) as refused:
                admit(store, "rejected.opt", OPT, resumable=resumable)
            assert refused.value.status_code == (422 if outcome == "infected" else 503)
            assert scanner.calls == [OPT]
            assert not store.documents and not list(store.files.iterdir())
            quarantined = list(store.quarantine_root.iterdir())
            if outcome == "infected":
                assert len(quarantined) == 1
                assert quarantined[0].read_bytes() == OPT
                assert stat.S_IMODE(quarantined[0].stat().st_mode) == 0o600
            else:
                assert quarantined == []
    store = PilotStore(tmp_path / "missing", malware_scanner=None, malware_scan_mode="extended")
    try:
        with pytest.raises(UploadProblem) as missing:
            admit(store, "missing.dat", DAT, resumable=resumable)
        assert missing.value.status_code == 503 and not store.documents
    finally:
        store.close()
    with pilot(tmp_path / "failed-isolation", scanner=CountingScanner("infected")) as store:
        import os
        original_replace = os.replace
        def refuse_quarantine(source, destination, *args, **kwargs):
            if Path(destination).parent == store.quarantine_root:
                raise OSError("Generated quarantine refusal")
            return original_replace(source, destination, *args, **kwargs)
        with monkeypatch.context() as patch:
            patch.setattr(os, "replace", refuse_quarantine)
            with pytest.raises(UploadProblem) as rejected:
                admit(store, "blocked.dat", DAT, resumable=resumable)
        assert rejected.value.status_code == 503
        assert "isolation did not finish" in str(rejected.value)
        assert not store.documents and not list(store.files.iterdir())


def test_load_file_http_selection_and_rejection(tmp_path):
    scanner = CountingScanner()
    policy = StoragePolicy(reserve_bytes=0, document_file_bytes=4096, media_file_bytes=4096,
                           upload_session_bytes=8192, matter_quota_bytes=8192)
    with TestClient(app_at(tmp_path / "runtime", scanner, background=True, policy=policy)) as client:
        bench, matter = new_matter(client)
        setup = client.get(f"/matters/{matter.slug}/setup")
        rendered = html.unescape(setup.text)
        assert SUPPORT_TEXT in rendered and LOAD_FILE_INTAKE_TEXT in rendered
        assert "4 KiB" in rendered
        for selector in ("source-files", "source-folder"):
            tag = re.search(r'<input\b[^>]*\bid="' + selector + r'"[^>]*>', rendered)
            assert tag is not None
            accepts = re.search(r'\baccept="([^"]+)"', tag.group()).group(1).split(",")
            assert {".dat", ".opt"} <= {value.strip() for value in accepts}
        selected = client.post(f"/matters/{matter.slug}/upload-preflight", json={"files": [
            descriptor("one.DAT", len(DAT)), descriptor("Folder/two.OPT", len(OPT)),
            descriptor("opaque.unsupported", 10), descriptor("large.dat", 4097),
            descriptor("../unsafe.dat", 10)]})
        assert selected.status_code == 200, selected.text
        items = selected.json()["items"]
        assert [item["eligible"] for item in items] == [True, True, False, False, False]
        assert all(item["scan"]["result"] == "not_run" for item in items[:2])
        assert scanner.calls == []
        for name, body in (("one.DAT", DAT), ("Folder/two.OPT", OPT)):
            receipt, session, item = receipt_upload(client, matter, name, body)
            response = client.post(item["finalize_url"])
            assert response.status_code == 200, response.text
            stored = next(d for d in bench.source_store(matter).documents.values()
                          if d.relative_path == name)
            exact_original(bench.source_store(matter), stored, body)
            assert client.get(receipt["receipt_url"]).status_code == 200
            assert client.get(session["status_url"]).json()["items"][0]["state"] == "queued"
        assert scanner.calls == [DAT, OPT]
        quota = client.post(f"/matters/{matter.slug}/upload-sessions", json={"files": [
            descriptor(f"quota-{index}.dat", 4000) for index in range(3)]})
        assert quota.status_code == 413
        invalid = client.post(f"/matters/{matter.slug}/uploads", files=[
            ("files", ("malformed.dat", b"A\x14B\none\x14two\x14three\n", "text/plain"))])
        assert invalid.status_code == 422
        assert LOAD_FILE_REJECTION_PREFIX in invalid.text and SUPPORT_TEXT in invalid.text
        assert len(bench.source_store(matter).documents) == 2


def javascript_declarations(*names):
    from tree_sitter import Language, Parser
    import tree_sitter_javascript
    source = (ROOT / "src/case_intelligence/static/case-intelligence.js").read_bytes()
    tree = Parser(Language(tree_sitter_javascript.language())).parse(source)
    wanted = set(names)
    found = {}
    pending = [tree.root_node]
    while pending:
        node = pending.pop()
        if node.type == "variable_declarator":
            name = node.child_by_field_name("name").text.decode()
            if name in wanted:
                found[name] = node.parent.text.decode()
        pending.extend(node.named_children)
    assert found.keys() == wanted
    return found


def test_load_file_javascript_selection_executes_real_declarations():
    found = javascript_declarations("validateSelectedFiles", "securityCheckedSuffixes", "requiresSecurityCheck")
    program = "\n".join([
        "const assert = require('node:assert/strict');",
        "const maximumUploadItems=3, maximumMediaFileBytes=4096, maximumUploadFileBytes=1024;",
        "const mediaLimitLabel='4 KiB', documentLimitLabel='1 KiB';",
        found["securityCheckedSuffixes"], found["requiresSecurityCheck"], found["validateSelectedFiles"],
        "for (const file of " + json.dumps([
            {"name": f"generated.{suffix}", "webkitRelativePath": prefix + f"generated.{suffix}", "size": 40}
            for suffix in ("dat", "DAT", "opt", "OPT") for prefix in ("", "Generated/")
        ]) + ") {",
        " assert.equal(validateSelectedFiles([file]),40);",
        " assert.equal(requiresSecurityCheck(file.name),true);",
        "}",
        "assert.throws(()=>validateSelectedFiles([{name:'bad.unsupported',size:1}]));",
        "assert.throws(()=>validateSelectedFiles([{name:'empty.dat',size:0}]));",
        "assert.throws(()=>validateSelectedFiles([{name:'large.opt',size:1025}]));",
        "assert.throws(()=>validateSelectedFiles(" + json.dumps([
            {"name": f"generated-{index}.dat", "size": 1} for index in range(4)
        ]) + "));",
        "assert.throws(()=>validateSelectedFiles([{name:'one.dat',size:1},{name:'ONE.dat',size:1}]));",
        "assert.equal(validateSelectedFiles([{name:'control.txt',size:1}]),1);",
        "assert.equal(requiresSecurityCheck('generated.dat','disabled'),false);",
        "assert.equal(requiresSecurityCheck('generated.opt','disabled'),false);",
        "assert.equal(requiresSecurityCheck('generated.txt','all'),true);",
        "assert.equal(requiresSecurityCheck('generated.txt','extended'),false);",
    ])
    result = subprocess.run(["node", "-e", program], text=True, capture_output=True, timeout=15)
    assert result.returncode == 0, result.stderr


def test_load_file_rejection_reason_bound_and_display(tmp_path):
    assert LOAD_FILE_SUPPORT_TEXT == SUPPORT_TEXT
    assert len(SUPPORT_TEXT) > 240
    workspace = WorkspaceStore(tmp_path / "workspace.sqlite")
    actor = workspace.upsert_principal("test", "synthetic-owner", "Generated owner", "generated")
    matter = workspace.create_matter("Generated durable reasons", "", actor.principal_id)
    reasons = []
    try:
        for code, diagnostic in DIAGNOSTICS.items():
            assert diagnostic.isascii() and len(diagnostic) <= 160
            for line in (None, 1, 10**20 - 1):
                error = LoadFileAdmissionError(code, line)
                location = "file" if line is None else f"line {line}"
                expected = f"DAT/OPT format rejected: {diagnostic} Location: {location}."
                assert error.durable_reason == expected
                assert len(expected) <= 222
                assert str(error).startswith(LOAD_FILE_REJECTION_PREFIX)
                assert SUPPORT_TEXT in str(error) and diagnostic in str(error) and location in str(error)
                assert display_load_file_reason(expected) == str(error)
                session, items = workspace.create_upload_session(matter.matter_id, actor.principal_id,
                    "Generated rejection", [{"display_name": "synthetic.dat", "relative_path": "synthetic.dat",
                        "media_type": "text/plain", "expected_size": 20}])
                workspace.fail_upload_item(matter.matter_id, actor.principal_id,
                    session.upload_session_id, items[0].upload_item_id, expected)
                reasons.append((session.upload_session_id, items[0].upload_item_id, expected))
        for unrelated in ("Generated independent failure", "DAT/OPT format rejected: unknown Location: file.",
                          reasons[0][2] + "extra", "DAT/OPT format rejected: injected\nLocation: file."):
            assert display_load_file_reason(unrelated) == unrelated
    finally:
        workspace.close()
    reopened = WorkspaceStore(tmp_path / "workspace.sqlite")
    try:
        for session_id, item_id, expected in reasons:
            item = reopened.upload_item(matter.matter_id, actor.principal_id, session_id, item_id)
            assert item.state == "failed" and item.message == expected
            assert reopened.upload_session_record(matter.matter_id, actor.principal_id, session_id).state == "partial"
    finally:
        reopened.close()


@pytest.mark.parametrize("kind", ["malformed-dat", "malformed-opt", "injected-finalization"])
def test_long_load_file_rejection_is_terminal_after_reopen(tmp_path, monkeypatch, kind):
    runtime = tmp_path / "runtime"
    suffix = "opt" if kind == "malformed-opt" else "dat"
    body = b"too,few,fields\n" if suffix == "opt" else b"A\x14B\none\x14two\x14three\n"
    if kind == "injected-finalization":
        body = b"Generated ordinary text upload.\n"
        suffix = "txt"
    with TestClient(app_at(runtime, background=True)) as client:
        bench, matter = new_matter(client)
        receipt, session, item = receipt_upload(client, matter, "Generated/rejected." + suffix, body)
        source_store = bench.source_store(matter)
        if kind == "injected-finalization":
            from case_intelligence.pilot_uploads import LoadFileUploadProblem
            failure = LoadFileUploadProblem(LoadFileAdmissionError(next(iter(DIAGNOSTICS)), 3))
            assert isinstance(failure, UploadProblem) and len(str(failure)) > 240
            def reject_finalization(*args, **kwargs):
                raise failure
            with monkeypatch.context() as patch:
                patch.setattr(source_store, "finalize_resumable_upload", reject_finalization)
                response = client.post(item["finalize_url"])
        else:
            response = client.post(item["finalize_url"])
        assert response.status_code == 422, response.text
        full = response.json()["message"]
        assert len(full) > 240 and full.startswith(LOAD_FILE_REJECTION_PREFIX)
        assert SUPPORT_TEXT in full and str(runtime) not in full
        durable = bench.workspace.upload_item(matter.matter_id, ACTOR,
            session["upload_session_id"], item["upload_item_id"])
        assert durable.state == "failed" and len(durable.message) <= 222
        assert display_load_file_reason(durable.message) == full
        short = durable.message
        displayed = short if kind == "injected-finalization" else full
        assert not source_store.documents and not list(source_store.files.iterdir())
        assert not list(source_store.incoming.iterdir())
        assert not list(source_store.staging.iterdir())
        refresh = client.get(session["status_url"]).json()
        assert refresh["state"] == "partial" and refresh["processing_count"] == 0
        assert refresh["items"][0]["message"] == displayed
        assert refresh["items"][0]["work_stage"] == displayed
        assert displayed in render_upload_status(refresh)["errors"][0]
        receipts = IntakeReceipts(bench.workspace)
        projected = receipts.items(matter.matter_id, ACTOR, receipt["receipt_id"])
        assert projected[0]["availability"] == "failed"
        assert receipts.get(matter.matter_id, ACTOR, receipt["receipt_id"])["counts"]["processing"] == 0
        assert displayed in html.unescape(client.get(receipt["receipt_url"]).text)
        if kind != "injected-finalization":
            direct = client.post(f"/matters/{matter.slug}/uploads", files=[
                ("files", ("direct." + suffix, body, "text/plain"))])
            assert direct.status_code == 422 and direct.text == full
    with TestClient(app_at(runtime, background=True)) as client:
        bench = client.app.state.workbench
        current = bench.workspace.upload_item(matter.matter_id, ACTOR,
            session["upload_session_id"], item["upload_item_id"])
        assert current.state == "failed" and current.message == short
        refresh = client.get(session["status_url"]).json()
        assert refresh["state"] == "partial" and refresh["processing_count"] == 0
        assert refresh["items"][0]["message"] == refresh["items"][0]["work_stage"] == displayed
        assert displayed in render_upload_status(refresh)["errors"][0]
        assert displayed in html.unescape(client.get(receipt["receipt_url"]).text)
        assert not list(bench.source_store(matter).incoming.iterdir())


@pytest.mark.parametrize("name,body", [("stored.dat", DAT), ("stored.opt", OPT)])
def test_load_file_is_stored_without_preview(tmp_path, monkeypatch, name, body):
    runtime = tmp_path / "runtime"
    with TestClient(app_at(runtime)) as client:
        bench, matter = new_matter(client)
        store = bench.source_store(matter)
        def forbid_text_materialization(*args, **kwargs):
            raise AssertionError("Stored load files must not reach generic text extraction")
        with monkeypatch.context() as patch:
            patch.setattr(store, "_read_regular_file", forbid_text_materialization)
            document = admit(store, name, body, deferred=True)
            assert document.state == "queued"
            deny_content(client, matter, document)
            document = store.process(document.document_id)
        exact_original(store, document, body)
        assert document.state == "ready" and document.message == LOAD_FILE_COMPLETION_MESSAGE
        deny_content(client, matter, document)
        token = store.action_token(document)
        source = client.get(f"/matters/{matter.slug}/sources/{token}")
        assert source.status_code == 200
        assert "SYN-0001" not in source.text
        for view in ("document", "text"):
            response = client.get(f"/matters/{matter.slug}/production/items/{token}", params={"view": view})
            assert response.status_code == 200, response.text
            assert "<iframe" not in response.text
            if view == "text":
                assert "SYN-0001" not in response.text
            assert f"/sources/{token}/content" not in response.text
    with TestClient(app_at(runtime)) as client:
        current = client.app.state.workbench.source_store(matter).get(document.document_id)
        exact_original(client.app.state.workbench.source_store(matter), current, body)
        deny_content(client, matter, current)


def test_load_file_restart_and_existing_lifecycle(tmp_path):
    runtime = tmp_path / "runtime"
    scanner = CountingScanner()
    with TestClient(app_at(runtime, scanner)) as client:
        bench, matter = new_matter(client)
        response = client.post(f"/matters/{matter.slug}/uploads", files=[
            ("files", ("retained.dat", DAT, "text/plain"))], follow_redirects=False)
        assert response.status_code == 303, response.text
        document = next(iter(bench.source_store(matter).documents.values()))
        identity = (document.document_id, document.version_id, document.digest)
        original_path = bench.source_store(matter).files / document.stored_name
    with TestClient(app_at(runtime, scanner)) as client:
        bench = client.app.state.workbench
        store = bench.source_store(matter)
        current = store.get(identity[0])
        exact_original(store, current, DAT)
        repeated = admit(store, "retained.dat", DAT)
        assert (repeated.document_id, repeated.version_id, repeated.digest) == identity
        assert scanner.calls == [DAT]
        foreign = bench.create_matter("Generated other matter", "", "development-jordan-lee")
        token = store.action_token(current)
        assert client.get(f"/matters/{foreign.slug}/sources/{token}/content").status_code == 404
        denied = client.post(f"/matters/{foreign.slug}/sources/{token}/remove", follow_redirects=False)
        assert denied.status_code == 404 and original_path.read_bytes() == DAT
        response = client.post(f"/matters/{matter.slug}/sources/{token}/remove", follow_redirects=False)
        assert response.status_code == 303, response.text
        assert not original_path.exists() and not store.documents
        with pytest.raises(KeyError):
            bench.workspace.source_catalog_record(matter.matter_id, document.document_id)
    with pilot(tmp_path / "changed") as store:
        document = admit(store, "changed.opt", OPT)
        altered = OPT.replace(b"SYN", b"BAD")
        (store.files / document.stored_name).write_bytes(altered)
        identity = (document.document_id, document.version_id, document.digest)
    with pilot(tmp_path / "changed") as store:
        changed = store.get(identity[0])
        assert changed.state == "changed" and changed.units == []
        assert (changed.document_id, changed.version_id, changed.digest) == identity
        with pytest.raises(UploadProblem) as retry:
            store.retry(changed.document_id)
        assert retry.value.status_code == 409
    recovery_scanner = CountingScanner()
    recovery_root = tmp_path / "resumable-recovery"
    with pilot(recovery_root, scanner=recovery_scanner) as store:
        recovery_item = put_resumable(store, DAT)
        first = store.finalize_resumable_upload(recovery_item, display_name="recovered.dat",
            relative_path="recovered.dat", content_type="text/plain", expected_size=len(DAT))
        recovery_identity = (first.document_id, first.version_id, first.digest)
    with pilot(recovery_root, scanner=recovery_scanner) as store:
        recovered = store.finalize_resumable_upload(recovery_item, display_name="recovered.dat",
            relative_path="recovered.dat", content_type="text/plain", expected_size=len(DAT))
        assert (recovered.document_id, recovered.version_id, recovered.digest) == recovery_identity
        assert len(store.documents) == len(list(store.files.iterdir())) == 1
        assert recovery_scanner.calls == [DAT]
        store.discard_resumable_upload(recovery_item)
        exact_original(store, store.process(recovered.document_id), DAT)


@pytest.mark.parametrize("mixed", [False, True], ids=["load-files-only", "mixed-text"])
def test_load_file_http_coverage_survives_completion_and_restart(tmp_path, monkeypatch, mixed):
    runtime = tmp_path / "runtime"
    entered, release = threading.Event(), threading.Event()
    original_process = PilotStore.process
    def observed_process(store, document_id, **kwargs):
        entered.set()
        if not release.wait(10):
            raise AssertionError("Generated ingestion barrier was not released")
        return original_process(store, document_id, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(PilotStore, "process", observed_process)
        with TestClient(app_at(runtime, background=True)) as client:
            bench, matter = new_matter(client)
            uploads = [("files", ("ready.DAT", DAT, "text/plain")),
                       ("files", ("ready.OPT", OPT, "text/plain"))]
            if mixed:
                uploads.append(("files", ("searchable.txt", b"Generated searchable copper note.\n", "text/plain")))
            try:
                response = client.post(f"/matters/{matter.slug}/uploads", files=uploads,
                                       follow_redirects=False)
                assert response.status_code == 303, response.text
                assert entered.wait(5)
                processing = bench.workspace.matter_readiness(matter.matter_id)
                assert processing.processing_count > 0 and not processing.can_query
                for document in bench.source_store(matter).documents.values():
                    if document.relative_path.endswith((".DAT", ".OPT")):
                        deny_content(client, matter, document)
            finally:
                release.set()
            identifiers = tuple(bench.source_store(matter).documents)
            wait_completed(bench, matter, identifiers)
            counts = bench.workspace.matter_readiness(matter.matter_id)
            assert counts.total_count == counts.saved_count == 2 + int(mixed)
            assert counts.extracted_count == counts.searchable_count == int(mixed)
            assert counts.processing_count == 0
            assert counts.attention_count == counts.stored_only_count == 2
            assert counts.can_query is mixed and counts.partial_query is mixed
            page = html.unescape(client.get(f"/matters/{matter.slug}").text)
            assert ("1 of 3 sources searchable" if mixed else "0 of 2 sources searchable") in page
            if not mixed:
                assert LOAD_FILE_COVERAGE_MESSAGE in page
                assert "Retry or remove an affected source to begin" not in page
            setup = html.unescape(client.get(f"/matters/{matter.slug}/setup").text)
            assert LOAD_FILE_COMPLETION_MESSAGE in setup
    with TestClient(app_at(runtime, background=True)) as client:
        bench = client.app.state.workbench
        counts = bench.workspace.matter_readiness(matter.matter_id)
        assert counts.saved_count == 2 + int(mixed)
        assert counts.extracted_count == counts.searchable_count == int(mixed)
        assert counts.stored_only_count == counts.attention_count == 2 and counts.processing_count == 0
        page = html.unescape(client.get(f"/matters/{matter.slug}").text)
        if not mixed:
            assert LOAD_FILE_COVERAGE_MESSAGE in page
        for document in bench.source_store(matter).documents.values():
            if document.relative_path.endswith((".DAT", ".OPT")):
                deny_content(client, matter, document)


def test_load_file_validator_refuses_symlinks_and_damaged_final_records(tmp_path):
    source = tmp_path / "generated.dat"
    source.write_bytes(DAT)
    validate_load_file(source, suffix=".dat")
    link = tmp_path / "linked.dat"
    link.symlink_to(source)
    with pytest.raises(LoadFileAdmissionError):
        validate_load_file(link, suffix=".dat")
    source.write_bytes(DAT + b"late\x14malformed\x14record\n")
    with pytest.raises(LoadFileAdmissionError) as rejected:
        validate_load_file(source, suffix=".dat")
    assert rejected.value.line is not None and rejected.value.line >= 3
    assert str(tmp_path) not in str(rejected.value)


def test_load_file_validation_rejects_changed_staging(tmp_path, monkeypatch):
    from case_intelligence.source_admission import load_files as admission
    source = tmp_path / "changing.dat"
    source.write_bytes(DAT)
    original_parser = admission.parse_dat
    def parse_then_change(path, **kwargs):
        yield from original_parser(path, **kwargs)
        with Path(path).open("ab") as stream:
            stream.write(b"\n")
    monkeypatch.setattr(admission, "parse_dat", parse_then_change)
    with pytest.raises(LoadFileAdmissionError) as rejected:
        validate_load_file(source, suffix=".dat")
    assert rejected.value.code == "changed_file"
    assert source.read_bytes() == DAT + b"\n"


def render_upload_status(payload):
    declarations = javascript_declarations("renderUpload", "requiresSecurityCheck", "securityCheckedSuffixes")
    program = "\n".join([
        "const uploadProgress={},uploadStatus={},uploadTitle={},uploadCount={};",
        "const uploadProgressBar=null,uploadBytes=null,uploadCancel=null,uploadReview=null;",
        "const uploadBatches=[],activeUploadBatchIndex=0,selectedUploadTotalBytes=0;",
        "const uploadForm={dataset:{malwareScanMode:'extended'}}; let activeUpload;",
        "const uploadErrors={rows:[],replaceChildren(){this.rows=[];},append(row){this.rows.push(row);}};",
        "const document={createElement(){return {textContent:''};}};",
        declarations["securityCheckedSuffixes"], declarations["requiresSecurityCheck"],
        declarations["renderUpload"],
        "renderUpload(" + json.dumps(payload) + ");",
        "console.log(JSON.stringify({status:uploadStatus.textContent,title:uploadTitle.textContent,",
        " errors:uploadErrors.rows.map(row=>row.textContent)}));",
    ])
    result = subprocess.run(["node", "-e", program], text=True, capture_output=True, timeout=15)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@pytest.mark.parametrize("outcome", ["stored", "failed", "changed", "missing"])
def test_load_file_resumable_polling_preserves_stored_and_integrity_states(tmp_path, monkeypatch, outcome):
    runtime = tmp_path / "runtime"
    entered, release = threading.Event(), threading.Event()
    original_process = PilotStore.process
    def observed_process(store, document_id, **kwargs):
        entered.set()
        if not release.wait(10):
            raise AssertionError("Generated resumable barrier was not released")
        document = original_process(store, document_id, **kwargs)
        if outcome == "failed":
            raise UploadProblem("Generated processing failure after stored-only extraction")
        return document
    with monkeypatch.context() as patch:
        patch.setattr(PilotStore, "process", observed_process)
        with TestClient(app_at(runtime, background=True)) as client:
            bench, matter = new_matter(client)
            receipt, session, item = receipt_upload(client, matter, "Generated/polling.dat", DAT)
            try:
                response = client.post(item["finalize_url"])
                assert response.status_code == 200, response.text
                assert response.json()["delta"] is True
                assert entered.wait(5)
                for response in (client.get(session["status_url"]),
                                 client.get(session["status_url"], params={"compact": "1"}),
                                 client.post(item["finalize_url"])):
                    assert response.status_code == 200, response.text
                    payload = response.json()
                    assert payload["processing_count"] == 1
                    assert payload["ready_count"] == payload["attention_count"] == 0
                    assert payload["work_complete"] is False
            finally:
                release.set()
            store = bench.source_store(matter)
            document = next(iter(store.documents.values()))
            wait_completed(bench, matter, (document.document_id,),
                           expected="failed" if outcome == "failed" else "succeeded")
            for response in (client.get(session["status_url"]),
                             client.get(session["status_url"], params={"compact": "1"}),
                             client.post(item["finalize_url"])):
                assert response.status_code == 200, response.text
                payload = response.json()
                assert payload["ready_count"] == payload["processing_count"] == 0
                assert payload["attention_count"] == 1 and payload["work_complete"] is True
                for projected in payload["items"]:
                    assert projected["work_state"] == "attention"
                    if outcome != "failed":
                        assert projected["work_stage"] == LOAD_FILE_COMPLETION_MESSAGE
                    else:
                        assert projected["work_stage"] != LOAD_FILE_COMPLETION_MESSAGE
                browser = render_upload_status(payload)
                assert "Every source is searchable" not in browser["status"]
                assert browser["title"] != "Sources ready"
            original = store.files / document.stored_name
    if outcome == "changed":
        original.write_bytes(DAT.replace(b"SYN", b"BAD"))
    elif outcome == "missing":
        original.unlink()
    with TestClient(app_at(runtime, background=True)) as client:
        bench = client.app.state.workbench
        current = bench.source_store(matter).get(document.document_id)
        assert current.state == (outcome if outcome in {"changed", "missing", "failed"} else "ready")
        for response in (client.get(session["status_url"]),
                         client.get(session["status_url"], params={"compact": "1"}),
                         client.post(item["finalize_url"])):
            assert response.status_code == 200, response.text
            payload = response.json()
            assert payload["ready_count"] == payload["processing_count"] == 0
            assert payload["attention_count"] == 1
            for projected in payload["items"]:
                assert projected["work_state"] == "attention"
                if outcome in {"changed", "missing"}:
                    assert projected["work_stage"] == current.message
                    assert LOAD_FILE_COMPLETION_MESSAGE not in projected["work_stage"]
            browser = render_upload_status(payload)
            assert "Every source is searchable" not in browser["status"]
        deny_content(client, matter, current)
        assert client.get(receipt["receipt_url"]).status_code == 200


@contextmanager
def forbid_path_io(forbidden, *, metadata=False):
    import builtins
    def guarded(original):
        def call(path, *args, **kwargs):
            if isinstance(path, (str, bytes, os.PathLike)):
                candidate = os.fsdecode(path)
                if candidate == os.fspath(forbidden):
                    raise AssertionError("An inert or protected original path was accessed")
            return original(path, *args, **kwargs)
        return call
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(builtins, "open", guarded(builtins.open))
        patch.setattr(io, "open", guarded(io.open))
        patch.setattr(os, "open", guarded(os.open))
        if metadata:
            patch.setattr(os, "stat", guarded(os.stat))
            patch.setattr(os, "lstat", guarded(os.lstat))
        yield


@pytest.mark.parametrize("suffix", ["dat", "opt"])
@pytest.mark.parametrize("resumable", [False, True])
def test_load_file_embedded_locations_remain_inert(tmp_path, suffix, resumable):
    sentinel = tmp_path / "generated-neighbor.txt"
    sentinel.write_bytes(b"Generated neighboring source must remain unopened")
    body = (f"Control\x14Native path\nSYN-1\x14{sentinel}\n" if suffix == "dat" else
            f"SYN-1,VOL,{sentinel},Y,,,1\n").encode()
    with pilot(tmp_path / "sources") as store:
        with forbid_path_io(sentinel, metadata=True):
            document = admit(store, "inert." + suffix, body, resumable=resumable)
        exact_original(store, document, body)
    assert sentinel.read_bytes() == b"Generated neighboring source must remain unopened"
