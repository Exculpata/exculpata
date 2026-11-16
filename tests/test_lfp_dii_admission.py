"""Generated image-index admission through actual browser/server boundaries.

The proposed grammar is deliberately narrow. These tests establish preserved
originals and admission, not later s4 interpretation or rendering.
"""
from __future__ import annotations

import hashlib
import html
import os
from pathlib import Path
import stat
import json
import re
import subprocess
import threading

import pytest

from fastapi.testclient import TestClient

from case_intelligence import source_admission as admission
from case_intelligence.intake_receipts import IntakeReceipts
from case_intelligence.managed_storage import StoragePolicy
from case_intelligence.malware_scan import MalwareScanResult
from case_intelligence.modules import ModuleDatabase
from case_intelligence.modules.discovery_review.production import ProductionRepository
from case_intelligence.pilot_uploads import PilotStore, UploadProblem, MAX_UPLOAD_SESSION_ITEMS
from tests.fixtures.load_files.generate_image_indexes import dii_bytes, lfp_bytes, minimal_image_index
from tests.test_load_file_admission import (
    ACTOR, CountingScanner, admit, app_at, deny_content, descriptor, exact_original,
    forbid_path_io, javascript_declarations, new_matter, pilot, receipt_upload,
    render_upload_status, wait_completed,
)


@pytest.fixture(autouse=True)
def image_index_upload_storage(monkeypatch):
    monkeypatch.setenv("CASE_INTELLIGENCE_STORAGE_RESERVE_GIB", "0")


def test_lfp_dii_selection_contract(tmp_path):
    declarations = javascript_declarations("validateSelectedFiles", "securityCheckedSuffixes",
                                           "requiresSecurityCheck", "reviewSuffixes", "mediaSuffixes")
    descriptors = [
        {"name": "arbitrary.lfp", "webkitRelativePath": "", "size": 24},
        {"name": "arbitrary.LFP", "webkitRelativePath": "Generated/arbitrary.LFP", "size": 24},
        {"name": "separate.dii", "webkitRelativePath": "", "size": 54},
        {"name": "separate.DII", "webkitRelativePath": "Nested/separate.DII", "size": 54},
        {"name": "retained.dat", "webkitRelativePath": "", "size": 24},
        {"name": "retained.OPT", "webkitRelativePath": "Generated/retained.OPT", "size": 24},
    ]
    program = "\n".join([
        "const assert=require('node:assert/strict');",
        "const maximumUploadItems=3, maximumMediaFileBytes=4096, maximumUploadFileBytes=128;",
        "const mediaLimitLabel='4 KiB',documentLimitLabel='128 B';",
        declarations["securityCheckedSuffixes"], declarations["requiresSecurityCheck"],
        declarations["reviewSuffixes"], declarations["mediaSuffixes"], declarations["validateSelectedFiles"],
        f"const cases={json.dumps(descriptors)};",
        "for (const file of cases) {",
        " assert.equal(validateSelectedFiles([file]),file.size);",
        " assert.equal(requiresSecurityCheck(file.name,'extended'),true);",
        " assert.equal(requiresSecurityCheck(file.name,'all'),true);",
        " assert.equal(requiresSecurityCheck(file.name,'disabled'),false);",
        "}",
        "assert.equal(reviewSuffixes.includes('lfp'),true);assert.equal(reviewSuffixes.includes('dii'),true);",
        "assert.equal(mediaSuffixes.includes('lfp'),false);assert.equal(mediaSuffixes.includes('dii'),false);",
        "assert.throws(()=>validateSelectedFiles([{name:'bad.unsupported',size:1}]));",
        "assert.throws(()=>validateSelectedFiles([{name:'empty.lfp',size:0}]));",
        "assert.throws(()=>validateSelectedFiles([{name:'large.dii',size:129}]));",
        "assert.throws(()=>validateSelectedFiles(cases));",
        "assert.throws(()=>validateSelectedFiles([{name:'one.lfp',size:1},{name:'ONE.lfp',size:1}]));",
        "assert.equal(validateSelectedFiles([{name:'control.txt',size:1}]),1);",
        "assert.equal(requiresSecurityCheck('control.txt','extended'),false);",
        "assert.equal(requiresSecurityCheck('control.txt','all'),true);",
    ])
    executed = subprocess.run(["node", "-e", program], text=True, capture_output=True, timeout=15)
    assert executed.returncode == 0, executed.stderr
    scanner = CountingScanner()
    policy = StoragePolicy(reserve_bytes=0, document_file_bytes=128, media_file_bytes=512,
                           upload_session_bytes=512, matter_quota_bytes=4096)
    with TestClient(app_at(tmp_path / "runtime", scanner, background=True, policy=policy)) as client:
        bench, matter = new_matter(client)
        setup = client.get(f"/matters/{matter.slug}/setup")
        assert setup.status_code == 200
        rendered = html.unescape(setup.text)
        assert admission.LOAD_FILE_INTAKE_TEXT in rendered
        assert admission.LOAD_FILE_SUPPORT_TEXT in rendered
        assert admission.IMAGE_LOAD_FILE_SUPPORT_TEXT in rendered
        for selector in ("source-files", "source-folder"):
            element = re.search(r'<input\b[^>]*\bid="' + selector + r'"[^>]*>', setup.text)
            assert element is not None
            values = re.search(r'\baccept="([^"]+)"', element.group()).group(1).split(",")
            assert {".lfp", ".dii", ".dat", ".opt"} <= set(values)
        files = [descriptor(item["webkitRelativePath"] or item["name"], item["size"]) for item in descriptors]
        files.extend((descriptor("bad.unsupported", 1), descriptor("oversized.lfp", 129),
                      descriptor("empty.dii", 0), descriptor("../unsafe.lfp", 24)))
        selected = client.post(f"/matters/{matter.slug}/upload-preflight", json={"files": files})
        assert selected.status_code == 200, selected.text
        rows = selected.json()["items"]
        assert [row["eligible"] for row in rows] == [True] * 6 + [False] * 4
        assert all(row["scan"]["result"] == "not_run" for row in rows[:6])
        assert not scanner.calls and not bench.source_store(matter).documents
        excessive = client.post(f"/matters/{matter.slug}/upload-sessions", json={"files": [
            descriptor(f"many-{number}.lfp", 1) for number in range(MAX_UPLOAD_SESSION_ITEMS + 1)]})
        assert excessive.status_code == 413
        assert str(MAX_UPLOAD_SESSION_ITEMS) in excessive.text.replace(",", "")
        duplicated = client.post(f"/matters/{matter.slug}/upload-preflight", json={"files": [
            descriptor("duplicate.lfp", 24), descriptor("DUPLICATE.lfp", 24)]})
        assert duplicated.status_code == 200
        assert [row["eligible"] for row in duplicated.json()["items"]] == [True, False]
        scanner.ready = False
        unavailable = client.post(f"/matters/{matter.slug}/upload-preflight", json={"files": files[:4]})
        assert unavailable.status_code == 200
        assert not any(row["eligible"] for row in unavailable.json()["items"])
        assert not scanner.calls and not bench.source_store(matter).documents



def malformed_inputs(source_format):
    """Independent literal bad cases, never expected values derived from production grammar."""
    valid = minimal_image_index(source_format)
    shared = [b"", b"ordinary text containing incidental IM marker\n", b"\n",
        b"MZ" + valid, b"\x7fELF" + valid, b"PK\x03\x04" + valid,
        b"<html><script>generated()</script></html>\n", valid + b"MZ\x90\x00generated\n",
        valid + b"PK\x03\x04generated\n", valid + b"\x7fELFgenerated\n",
        valid + b"#!/bin/sh\nexit\n", valid + b"<script>generated()</script>\n",
        valid + b"\x00", valid + b"\x01", valid + b"\x1b", valid + b"\xef\xbb\xbf"]
    if source_format == "lfp":
        rows = ["IM,KEY,D,0,one.tif;2", "IM,KEY,,0,one.png;4"]
        bad = ["im,KEY,D,0,one.tif;2", "OTHER,KEY,D,0,one.tif;2", "## comment",
            "IM,KEY,D,0", "IM,KEY,D,0,one.tif;2,0,extra", "IM,,D,0,one.tif;2",
            "IM,   ,D,0,one.tif;2", "IM," + "K" * 71 + ",D,0,one.tif;2",
            'IM,"KEY,one",D,0,one.tif;2', 'IM,KEY,D,0,"one;two.tif";2',
            "IM,KEY,D,0,one.tif;2\nIM,truncated", "IM,KEY,D,0,@V;;one.tif;2",
            "IM,KEY,D,0,@;Images;one.tif;2", "IM,KEY,D,0,@" + "V" * 31 + ";Images;one.tif;2",
            "IM,KEY,D,0,@VOL;Images;one.tif;2;surplus"]
        bad.extend(f"IM,KEY,{boundary},0,one.tif;2" for boundary in ("C", "S", "B", "F", "d", "  ", "arbitrary"))
        bad.extend(f"IM,KEY,D,{offset},one.tif;2" for offset in ("", "1", "-1", "+0", "00", "0.0", "０"))
        bad.extend(f"IM,KEY,D,0,one.tif;{kind}" for kind in ("", "1", "3", "TIFF", "02"))
        bad.extend(f"IM,KEY,D,0,one.tif;2,{rotation}" for rotation in ("", "4", "-1", "90", "01"))
        bad.extend(f"IM,KEY,D,0,{name};4" for name in
            ("../escape.png", "/absolute.png", "C:\\drive.png", "\\\\host\\share.png",
             "https://example.invalid/image.png", "*.png", "${HOME}.png", "$(touch sentinel).png"))
        bad.extend(f"IM,KEY,D,0,@VOL;{directory};image.png;4" for directory in
            ("../Images", "/Images", "C:\\Images", "\\\\host\\share", "Images/../Outside", "https://example.invalid"))
        shared.extend((text + "\n").encode() for text in bad)
        shared.append(("\n".join(rows) + "\nunknown tail\n").encode())
    else:
        base = "@FULLTEXT DOC\n@T record\n@BATESBEG SYN1\n@D @I\\\none.png\n"
        bad = [base.replace("@FULLTEXT DOC\n", ""), base.replace("DOC\n", "PAGE\n", 1),
            base.replace("@FULLTEXT DOC\n", "@FULLTEXT DOC\n@FULLTEXT DOC\n"),
            base.replace("@BATESBEG SYN1\n", ""), base.replace("@T record", "@T "),
            base.replace("@BATESBEG SYN1", "@BATESBEG "), base + "@END\n", base + "<<DOC>>\n",
            base.replace("@T record", "<<DOC>>"), base.replace("@D @I\\\n", "naked text\n@D @I\\\n"),
            base.replace("@D @I\\\n", "@MULTILINE\n@D @I\\\n"),
            base.replace("@D @I\\\n", "@UNKNOWN value\n@D @I\\\n"),
            base.replace("@D @I\\\n", "@EMAIL-BODY\n@T apparent\n@D @I\\\n"),
            base.replace("@D @I\\\n", ""), base.replace("one.png\n", ""),
            base.replace("@D @I\\\n", "@D @I\\\n@D @I\\\n"),
            base + "@DOCID after-images\n", base + "@T truncated\n", base + "\n",
            base.replace("@D @I\\\n", "@D @V\\Images\\\n"),
            base.replace("@D @I\\\n", "@D Images\n"),
            base.replace("@D @I\\\n", "@D @I\\..\\\n"),
            base.replace("@D @I\\\n", "@D @I\\C:\\Images\\\n")]
        for scalar in ("@BATESBEG SYN1", "@DOCID doc", "@PARENTID parent", "@PGCOUNT 1"):
            bad.append(base.replace("@D @I\\\n", f"{scalar}\n{scalar}\n@D @I\\\n"))
        bad.append(base.replace("@D @I\\\n", "@PGCOUNT 1\n@C PGCOUNT 1\n@D @I\\\n"))
        bad.extend(base.replace("@D @I\\\n", f"@PGCOUNT {count}\n@D @I\\\n") for count in
            ("", "0", "-1", "+1", "1.5", "١", "１", "9223372036854775808", "00000000000000000001"))
        bad.extend(base.replace("one.png", filename) for filename in
            ("../escape.png", "/absolute.png", "C:\\drive.png", "\\\\host\\share.png",
             "https://example.invalid/image.png", "*.png", "one/{1,2}.png", '"one.png"', "folder/one.png"))
        shared.extend(text.encode() for text in bad)
    return shared


@pytest.mark.parametrize("source_format", ["lfp", "dii"])
@pytest.mark.parametrize("resumable", [False, True], ids=["direct", "resumable"])
def test_admission_both_upload_paths(tmp_path, source_format, resumable):
    generator = lfp_bytes if source_format == "lfp" else dii_bytes
    valid = generator()
    scanner = CountingScanner()
    with pilot(tmp_path / "sources", scanner=scanner) as store:
        names = (f"arbitrary-name.{source_format}",
                 "production-other.DII" if source_format == "lfp" else "production-other.LFP",
                 f"Nested-record.{source_format}")
        for number, name in enumerate(names):
            document = admit(store, name, valid, resumable=resumable, deferred=bool(number % 2),
                             relative_path=f"Generated/Folder/{name}")
            exact_original(store, document, valid)
            if document.state == "queued":
                document = store.process(document.document_id)
            assert document.state == "ready" and document.message == admission.LOAD_FILE_COMPLETION_MESSAGE
            classified = admission.classify_image_load_file(store.files / document.stored_name)
            assert classified.format == source_format and classified.outcome == "recognized"
        for index, (encoding, newline) in enumerate((encoding, newline)
                for encoding in ("utf-8", "utf-8-sig", "cp1252", "utf-16-le", "utf-16-be")
                for newline in ("\n", "\r\n", "\r")):
            body = generator(encoding=encoding, newline=newline)
            document = admit(store, f"encoding-{index}.{source_format}", body, resumable=resumable)
            exact_original(store, document, body)
        if source_format == "lfp":
            optional = lfp_bytes((
                {"key": "  EDGE0001  ", "boundary": " ", "volume": "VOL",
                 "directory": "Generated\\Images", "filename": "one.tif", "image_type": "2", "rotation": "1"},
                {"key": "EDGE0007", "boundary": "D", "filename": "two.png", "image_type": "4", "rotation": "3"},
            ))
            exact_original(store, admit(store, "optional-values.lfp", optional, resumable=resumable), optional)
            prefix = b"IM,SYN1,D,0,one.png;4\n" * 4000
            many = prefix * 20
            oversized = b"IM,SYN1,D,0," + b"x" * (1024 * 1024 + 1) + b".png;4\n"
        else:
            optional = (b"@FULLTEXT DOC\n@T separate-record\n@PARENTID supplied-parent\n@PGCOUNT 0001\n"
                        b"@DOCID separate-document\n@BATESBEG  SYN0001 \n@D @I\\\none.png\n")
            exact_original(store, admit(store, "optional-values.dii", optional, resumable=resumable), optional)
            prefix = b"@FULLTEXT DOC\n@T record\n@BATESBEG SYN1\n@D @I\\\n" + b"one.png\n" * 10000
            many = b"@FULLTEXT DOC\n@T record\n@BATESBEG SYN1\n@D @I\\\n" + (b"x" * 250 + b".png\n") * 5000
            oversized = b"@FULLTEXT DOC\n@T " + b"x" * (1024 * 1024 + 1) + b"\n"
            huge = "x" * (300 * 1024)
            metadata_overflow = ("@FULLTEXT DOC\n@T " + huge + "\n@BATESBEG " + huge
                + "\n@DOCID " + huge + "\n@PARENTID " + huge + "\n@D @I\\\none.png\n").encode()
            with pytest.raises(UploadProblem):
                admit(store, "metadata-overflow.dii", metadata_overflow, resumable=resumable)
            count_edge = minimal_image_index("dii").replace(b"@D ", b"@PGCOUNT 9223372036854775807\n@D ")
            exact_original(store, admit(store, "literal-count.dii", count_edge, resumable=resumable), count_edge)
        assert len(prefix) > 65536 and len(many) > 1024 * 1024
        exact_original(store, admit(store, "many-records." + source_format, many, resumable=resumable), many)
        damaged = malformed_inputs(source_format)
        damaged.extend((valid.decode("utf-8").encode("utf-32"), valid.decode("utf-8").encode("utf-32-be"),
            valid.decode("utf-8").encode("utf-16-le"),
            prefix + b"\xff\n", prefix + b"\x81\n", generator(encoding="utf-16-le") + b"\x00", oversized))
        before = set(store.documents)
        for index, body in enumerate(damaged):
            with pytest.raises(UploadProblem) as rejected:
                admit(store, f"rejected-{index}.{source_format}", body, resumable=resumable, deferred=True)
            assert rejected.value.status_code in {400, 413, 422}
            assert str(tmp_path) not in str(rejected.value)
            assert set(store.documents) == before
            assert not list(store.staging.iterdir())
        assert len(list(store.files.iterdir())) == len(before)
    with pilot(tmp_path / "sources") as reopened:
        assert set(reopened.documents) == before
        assert all(document.units == [] and document.media_type == "text/plain" for document in reopened.documents.values())


@pytest.mark.parametrize("source_format", ["lfp", "dii"])
@pytest.mark.parametrize("resumable", [False, True], ids=["direct", "resumable"])
def test_admission_preserves_safety_gates(tmp_path, monkeypatch, source_format, resumable):
    body = minimal_image_index(source_format)
    for code, explanation in admission.IMAGE_DIAGNOSTICS.items():
        error = admission.LoadFileAdmissionError(code, 10 ** 20 - 1, format_group="LFP/DII")
        assert len(error.durable_reason) <= 222 and explanation.isascii()
        assert admission.display_load_file_reason(error.durable_reason) == str(error)
        assert admission.IMAGE_LOAD_FILE_SUPPORT_TEXT in str(error)
    for mode in ("all", "extended", "disabled"):
        scanner = CountingScanner()
        with pilot(tmp_path / mode, scanner=scanner, mode=mode) as store:
            document = admit(store, "generated." + source_format, body, resumable=resumable)
            exact_original(store, document, body)
            assert scanner.calls == ([] if mode == "disabled" else [body])
            assert "scanned" not in document.message.casefold()
    for outcome in ("unavailable", "error", "indeterminate", "infected"):
        scanner = CountingScanner(outcome, ready=outcome != "unavailable")
        with pilot(tmp_path / outcome, scanner=scanner) as store:
            with pytest.raises(UploadProblem) as rejected:
                admit(store, "blocked." + source_format, body, resumable=resumable)
            assert rejected.value.status_code == (422 if outcome == "infected" else 503)
            assert scanner.calls == [body]
            assert not store.documents and not list(store.files.iterdir()) and not list(store.staging.iterdir())
            quarantine = list(store.quarantine_root.iterdir())
            assert len(quarantine) == int(outcome == "infected")
            if quarantine:
                assert quarantine[0].read_bytes() == body and stat.S_IMODE(quarantine[0].stat().st_mode) == 0o600
    missing = PilotStore(tmp_path / "missing-scanner", malware_scanner=None, malware_scan_mode="extended")
    try:
        with pytest.raises(UploadProblem) as rejected:
            admit(missing, "missing." + source_format, body, resumable=resumable)
        assert rejected.value.status_code == 503 and not missing.documents
    finally:
        missing.close()
    with pilot(tmp_path / "mime") as store:
        for index, mime in enumerate((None, "", "text/plain", "text/plain; charset=windows-1252", "application/octet-stream")):
            document = admit(store, f"transport-{index}.{source_format}", body, resumable=resumable, media_type=mime)
            exact_original(store, document, body)
        for index, mime in enumerate(("text/html", "application/zip", "application/x-executable", "application/pdf")):
            with pytest.raises(UploadProblem, match="type does not match"):
                admit(store, f"mismatch-{index}.{source_format}", body, resumable=resumable, media_type=mime)
        for path in (f"../escape.{source_format}", f"/absolute.{source_format}", f"C:/drive.{source_format}",
                     f"nested/../escape.{source_format}", f"nested/CON.{source_format}",
                     f"nested/hidden\u202e.{source_format}", f"nested/control\x01.{source_format}"):
            with pytest.raises(UploadProblem):
                store.validate_relative_upload_path(path)
        assert len(store.documents) == 5
    with pilot(tmp_path / "limit", limit=len(body)) as store:
        exact_original(store, admit(store, "exact." + source_format, body, resumable=resumable), body)
        with pytest.raises(UploadProblem) as over:
            admit(store, "over." + source_format, body + b"\n", resumable=resumable)
        assert over.value.status_code == 413 and len(store.documents) == 1
    alternate = body.replace(b"SYN1", b"SYN2")
    assert alternate != body and len(alternate) == len(body)
    valid_alternate = tmp_path / ("alternate." + source_format)
    valid_alternate.write_bytes(alternate)
    admission.validate_load_file(valid_alternate, suffix="." + source_format)
    class ChangedScanner(CountingScanner):
        def scan(self, path):
            result = super().scan(path)
            path.write_bytes(alternate)
            return result
    with pilot(tmp_path / "changed-scan", scanner=ChangedScanner()) as store:
        with pytest.raises(UploadProblem):
            admit(store, "changing." + source_format, body, resumable=resumable)
        assert not store.documents and not list(store.files.iterdir()) and not list(store.staging.iterdir())
    with pilot(tmp_path / "quarantine-failure", scanner=CountingScanner("infected")) as store:
        original_replace = os.replace
        def refuse_quarantine(source, destination, *args, **kwargs):
            if Path(destination).parent == store.quarantine_root:
                raise OSError("Generated quarantine failure")
            return original_replace(source, destination, *args, **kwargs)
        with monkeypatch.context() as patch:
            patch.setattr(os, "replace", refuse_quarantine)
            with pytest.raises(UploadProblem) as rejected:
                admit(store, "isolated." + source_format, body, resumable=resumable)
        assert rejected.value.status_code == 503 and "isolation did not finish" in str(rejected.value)
        assert not store.documents and not list(store.files.iterdir())
    scan_live, scan_alternate, scan_saved = (tmp_path / name for name in
        ("scanner-live", "scanner-alternate", "scanner-saved"))
    scan_live.mkdir()
    scan_alternate.mkdir()
    scan_name = "same-name." + source_format
    (scan_live / scan_name).write_bytes(body)
    (scan_alternate / scan_name).write_bytes(alternate)
    scanned = []
    def scan_during_parent_swap(path):
        scan_live.rename(scan_saved)
        scan_alternate.rename(scan_live)
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            try:
                actual = os.read(descriptor, len(body) + 1)
            finally:
                os.close(descriptor)
            scanned.append(actual)
            return MalwareScanResult("infected" if actual == body else "clean", "synthetic",
                                     message="Generated verdict based on the bytes actually scanned")
        finally:
            scan_live.rename(scan_alternate)
            scan_saved.rename(scan_live)
    # A restored pathname and matching before/after hashes cannot substitute
    # for binding the actual scanner's no-follow read to the original directory.
    verdict = admission.scan_verified_upload(scan_live / scan_name,
        expected_digest=hashlib.sha256(body).hexdigest(), maximum_bytes=len(body), scan=scan_during_parent_swap)
    assert scanned == [body] and verdict.state == "infected"
    assert (scan_live / scan_name).read_bytes() == body
    assert (scan_alternate / scan_name).read_bytes() == alternate
    original = tmp_path / ("authorized." + source_format)
    original.write_bytes(body)
    link = tmp_path / ("linked." + source_format)
    link.symlink_to(original)
    with pytest.raises(admission.LoadFileAdmissionError):
        admission.validate_load_file(link, suffix="." + source_format)
    from case_intelligence.source_admission import load_files as validator
    original_lines = validator.iter_load_file_lines
    def change_after_read(path, **kwargs):
        yield from original_lines(path, **kwargs)
        with Path(path).open("ab") as changed:
            changed.write(b"\n")
    with monkeypatch.context() as patch:
        patch.setattr(validator, "iter_load_file_lines", change_after_read)
        with pytest.raises(admission.LoadFileAdmissionError) as changed:
            admission.validate_load_file(original, suffix="." + source_format)
        assert changed.value.code == "changed_file"
    # Rename parent directories without modifying the original file's own
    # inode/stat identity. Validation must keep reading the held original.
    live, alternate_dir, saved = (tmp_path / name for name in ("live", "alternate-parent", "saved-parent"))
    live.mkdir()
    alternate_dir.mkdir()
    invalid = body + b"@UNKNOWN generated-invalid-tail\n"
    original_name = "same-name." + source_format
    (live / original_name).write_bytes(invalid)
    (alternate_dir / original_name).write_bytes(body)
    before = (live / original_name).stat()
    redirected = []
    def swapped_parent_read(path, **kwargs):
        redirected.append(path)
        live.rename(saved)
        alternate_dir.rename(live)
        try:
            yield from original_lines(path, **kwargs)
        finally:
            live.rename(alternate_dir)
            saved.rename(live)
    with monkeypatch.context() as patch:
        patch.setattr(validator, "iter_load_file_lines", swapped_parent_read)
        with pytest.raises(admission.LoadFileAdmissionError):
            admission.validate_load_file(live / original_name, suffix="." + source_format)
    assert redirected
    after = (live / original_name).stat()
    assert (before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) == (
        after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
    assert (live / original_name).read_bytes() == invalid
    assert (alternate_dir / original_name).read_bytes() == body
    sentinel = tmp_path / "one.png"
    sentinel.write_bytes(b"Generated neighboring bytes must not be inspected")
    inert = (b"IM,SYN1,D,0,one.png;4\n" if source_format == "lfp" else
             b"@FULLTEXT DOC\n@T record\n@BATESBEG SYN1\n@D @I\\\none.png\n")
    with pilot(tmp_path / "inert") as store:
        with forbid_path_io(sentinel, metadata=True), forbid_path_io("one.png", metadata=True):
            exact = admit(store, "inert." + source_format, inert, resumable=resumable)
        exact_original(store, exact, inert)
    assert sentinel.read_bytes() == b"Generated neighboring bytes must not be inspected"


@pytest.mark.parametrize("source_format", ["lfp", "dii"])
@pytest.mark.parametrize("outcome", ["stored", "stored-mixed", "failed", "changed", "missing"])
def test_admitted_indexes_remain_stored_only(tmp_path, monkeypatch, source_format, outcome):
    runtime = tmp_path / "runtime"
    body = minimal_image_index(source_format)
    entered, release = threading.Event(), threading.Event()
    process = PilotStore.process

    def observed_process(store, document_id, **kwargs):
        entered.set()
        assert release.wait(10), "Generated worker barrier was not released"
        document = process(store, document_id, **kwargs)
        if outcome == "failed":
            raise UploadProblem("Generated worker failure after real stored-only processing")
        return document

    scanner = CountingScanner()
    with monkeypatch.context() as patch:
        patch.setattr(PilotStore, "process", observed_process)
        with TestClient(app_at(runtime, scanner, background=True)) as client:
            bench, matter = new_matter(client)
            receipt, session, item = receipt_upload(client, matter, "Generated/original." + source_format, body)
            store = bench.source_store(matter)
            try:
                response = client.post(item["finalize_url"])
                assert response.status_code == 200, response.text
                assert response.json()["delta"] is True
                assert entered.wait(5)
                document = next(iter(store.documents.values()))
                exact_original(store, document, body)
                for response in (client.get(session["status_url"]),
                                 client.get(session["status_url"], params={"compact": "1"}),
                                 client.post(item["finalize_url"])):
                    payload = response.json()
                    assert response.status_code == 200 and payload["processing_count"] == 1
                    assert payload["ready_count"] == payload["attention_count"] == 0
                    assert payload["work_complete"] is False
                deny_content(client, matter, document)
            finally:
                release.set()
            wait_completed(bench, matter, (document.document_id,), expected="failed" if outcome == "failed" else "succeeded")
            document = store.get(document.document_id)
            exact_original(store, document, body)
            for response in (client.get(session["status_url"]),
                             client.get(session["status_url"], params={"compact": "1"}),
                             client.post(item["finalize_url"])):
                assert response.status_code == 200, response.text
                payload = response.json()
                assert payload["ready_count"] == payload["processing_count"] == 0
                assert payload["attention_count"] == 1 and payload["work_complete"] is True
                assert all(row["work_state"] == "attention" for row in payload["items"])
                browser = render_upload_status(payload)
                assert "Every source is searchable" not in browser["status"] and browser["title"] != "Sources ready"
            if outcome.startswith("stored"):
                uploads = [("files", ("direct." + source_format.upper(), body, "application/octet-stream"))]
                if outcome == "stored-mixed":
                    uploads.append(("files", ("searchable.txt", b"Generated searchable copper text.\n", "text/plain")))
                response = client.post(f"/matters/{matter.slug}/uploads", files=uploads, follow_redirects=False)
                assert response.status_code == 303, response.text
                wait_completed(bench, matter, tuple(store.documents))
            counts = bench.workspace.matter_readiness(matter.matter_id)
            repository = ProductionRepository(ModuleDatabase(bench.workspace.connection, bench.workspace._lock))
            assert repository.attempts(matter.matter_id, include_stale=True) == ()
            for original in store.documents.values():
                assert repository.document_records(matter.matter_id, original.document_id) == ()
            assert counts.saved_count == (3 if outcome == "stored-mixed" else 2 if outcome == "stored" else 1)
            assert counts.searchable_count == counts.extracted_count == int(outcome == "stored-mixed")
            assert counts.processing_count == 0 and counts.can_query is (outcome == "stored-mixed")
            assert counts.attention_count == counts.saved_count - counts.searchable_count
            page = html.unescape(client.get(f"/matters/{matter.slug}").text)
            if outcome in {"stored", "stored-mixed"}:
                assert ("1 of 3 sources searchable" if outcome == "stored-mixed" else "0 of 2 sources searchable") in page
            if outcome == "stored":
                assert admission.LOAD_FILE_COVERAGE_MESSAGE in page
            for original in store.documents.values():
                if original.display_name.casefold().endswith((".lfp", ".dii")):
                    exact_original(store, original, body)
                    deny_content(client, matter, original)
                    source_page = client.get(f"/matters/{matter.slug}/sources/{store.action_token(original)}")
                    assert source_page.status_code == 200 and "<iframe" not in source_page.text
            # A real malformed finalization must be durably terminal, not merely
            # a long immediate error that leaves its receipt processing forever.
            malformed = body + b"@UNKNOWN DO-NOT-REFLECT-THIS-PAYLOAD\n"
            bad_receipt, bad_session, bad_item = receipt_upload(client, matter, "Generated/rejected." + source_format, malformed)
            rejected = client.post(bad_item["finalize_url"])
            assert rejected.status_code == 422, rejected.text
            full_reason = rejected.json()["message"]
            assert len(full_reason) > 240 and "DO-NOT-REFLECT-THIS-PAYLOAD" not in full_reason
            assert str(runtime) not in full_reason
            durable = bench.workspace.upload_item(matter.matter_id, ACTOR,
                bad_session["upload_session_id"], bad_item["upload_item_id"])
            assert durable.state == "failed" and len(durable.message) <= 222
            assert admission.display_load_file_reason(durable.message) == full_reason
            ledger = IntakeReceipts(bench.workspace)
            assert ledger.items(matter.matter_id, ACTOR, bad_receipt["receipt_id"])[0]["availability"] == "failed"
            assert ledger.get(matter.matter_id, ACTOR, bad_receipt["receipt_id"])["counts"]["processing"] == 0
            direct_bad = client.post(f"/matters/{matter.slug}/uploads", files=[
                ("files", ("direct-rejected." + source_format, malformed, "text/plain"))])
            assert direct_bad.status_code == 422 and direct_bad.text == full_reason
            assert not list(store.staging.iterdir()) and not list(store.incoming.iterdir())
            original_path = store.files / document.stored_name
            identity = document.document_id, document.version_id, document.digest
    if outcome == "changed":
        original_path.write_bytes(body.replace(b"SYN1", b"BAD1"))
    elif outcome == "missing":
        original_path.unlink()
    with TestClient(app_at(runtime, scanner, background=True)) as client:
        bench = client.app.state.workbench
        store = bench.source_store(matter)
        current = store.get(identity[0])
        assert (current.document_id, current.version_id, current.digest) == identity
        assert current.state == (outcome if outcome in {"changed", "missing", "failed"} else "ready")
        assert current.units == [] and current.units_file == "" and current.page_count == 0
        if outcome not in {"changed", "missing"}:
            exact_original(store, current, body)
        if outcome == "failed":
            source_page = client.get(f"/matters/{matter.slug}/sources/{store.action_token(current)}")
            assert source_page.status_code == 200 and current.message in html.unescape(source_page.text)
            assert bench.workspace.ingest_job(matter.matter_id, current.document_id).state == "failed"
        responses = (("full", client.get(session["status_url"])),
            ("compact", client.get(session["status_url"], params={"compact": "1"})),
            ("delta", client.post(item["finalize_url"])))
        for kind, response in responses:
            assert response.status_code == 200, response.text
            payload = response.json()
            assert payload["ready_count"] == payload["processing_count"] == 0
            assert payload["attention_count"] == 1
            if kind != "compact":
                assert len(payload["items"]) == 1
            for projected in payload["items"]:
                assert projected["work_state"] == "attention"
                if outcome in {"changed", "missing"}:
                    assert projected["work_stage"] == current.message
                elif outcome == "failed":
                    assert projected["work_stage"] and projected["work_stage"] != admission.LOAD_FILE_COMPLETION_MESSAGE
            assert "Every source is searchable" not in render_upload_status(payload)["status"]
        deny_content(client, matter, current)
        terminal = client.get(bad_session["status_url"]).json()
        assert terminal["state"] == "partial" and terminal["processing_count"] == 0
        assert terminal["items"][0]["message"] == terminal["items"][0]["work_stage"] == full_reason
        assert full_reason in html.unescape(client.get(bad_receipt["receipt_url"]).text)
        assert full_reason in render_upload_status(terminal)["errors"][0]
        if outcome == "stored":
            foreign = bench.create_matter("Generated foreign matter", "", "development-jordan-lee")
            token = store.action_token(current)
            denied = client.post(f"/matters/{foreign.slug}/sources/{token}/remove", follow_redirects=False)
            assert denied.status_code == 404 and original_path.read_bytes() == body
            removed = client.post(f"/matters/{matter.slug}/sources/{token}/remove", follow_redirects=False)
            assert removed.status_code == 303 and not original_path.exists()
            with pytest.raises(KeyError):
                bench.workspace.source_catalog_record(matter.matter_id, current.document_id)
