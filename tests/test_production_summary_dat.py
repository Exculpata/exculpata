"""Synthetic regressions for Concordance DAT support in Production Summary."""
from __future__ import annotations

import hashlib

import pytest

from case_intelligence import production_summary as summary
from tests.test_production_summary import HEADER
from tests.test_workflow_quality_handoff import pdf_bytes


def _dat_bytes(headers: tuple[str, ...], rows: list[tuple[str, ...]], *, encoding: str = "utf-8") -> bytes:
    fmt = lambda cells: "\x14".join(f"\u00fe{cell}\u00fe" for cell in cells)
    lines = [fmt(headers), *(fmt(row) for row in rows)]
    return ("\n".join(lines) + "\n").encode(encoding)


def test_parse_dat_load_file_utf8_cp1252_and_fail_closed(tmp_path):
    utf8_dat = tmp_path / "prod.dat"
    utf8_dat.write_bytes(_dat_bytes(
        ("BEGBATES", "ENDBATES", "NATIVELINK"),
        [("SYN0001", "SYN0003", "NATIVES\\a.pdf"), ("SYN0005", "SYN0005", "NATIVES\\b.pdf")],
    ))
    rows = summary.parse_dat_load_file(utf8_dat)
    assert rows == [
        {"path": "NATIVES\\a.pdf", "begin_bates": "SYN0001", "end_bates": "SYN0003"},
        {"path": "NATIVES\\b.pdf", "begin_bates": "SYN0005", "end_bates": "SYN0005"},
    ]

    cp1252_dat = tmp_path / "latin.dat"
    cp1252_dat.write_bytes(_dat_bytes(
        ("BEGDOC", "ENDDOC", "NATIVEPATH"),
        [("CAF0001", "CAF0001", "NATIVES\\caf\u00e9.pdf")],
        encoding="cp1252",
    ))
    latin_rows = summary.parse_dat_load_file(cp1252_dat)
    assert latin_rows[0]["path"] == "NATIVES\\caf\u00e9.pdf"

    # Producer-specific headings work via explicit field_map.
    custom = tmp_path / "custom.dat"
    custom.write_bytes(_dat_bytes(("START_NUM", "STOP_NUM", "FILE_REF"), [("DEF001", "DEF002", "a.pdf")]))
    mapped = summary.parse_dat_load_file(custom, field_map={"START_NUM": "begin_bates", "STOP_NUM": "end_bates", "FILE_REF": "native_path"})
    assert mapped == [{"path": "a.pdf", "begin_bates": "DEF001", "end_bates": "DEF002"}]

    # Missing required headers, ambiguous headers, or rejected rows fail closed.
    bad_hdr = tmp_path / "bad_hdr.dat"
    bad_hdr.write_bytes(_dat_bytes(("CUSTODIAN", "TITLE"), [("A", "B")]))
    with pytest.raises(ValueError):
        summary.parse_dat_load_file(bad_hdr)

    ambiguous = tmp_path / "ambiguous.dat"
    ambiguous.write_bytes(_dat_bytes(("BEGDOC", "BEGBATES", "NATIVELINK"), [("S1", "S2", "a.pdf")]))
    with pytest.raises(ValueError):
        summary.parse_dat_load_file(ambiguous)

    bad_row = tmp_path / "bad_row.dat"
    bad_row.write_bytes(_dat_bytes(("BEGDOC", "ENDDOC", "NATIVELINK"), [("S1", "S2")]))
    with pytest.raises(ValueError):
        summary.parse_dat_load_file(bad_row)


def test_http_production_summary_with_concordance_dat_and_bates_gaps(tmp_path, monkeypatch):
    monkeypatch.setenv("CASE_INTELLIGENCE_STORAGE_RESERVE_GIB", "0")
    monkeypatch.setenv("CASE_INTELLIGENCE_OCR_MODE", "disabled")
    from tests.test_discovery_review_dat_opt import opened_review

    dat_raw = _dat_bytes(
        ("BEGBATES", "ENDBATES", "NATIVELINK"),
        [
            ("PROD0001", "PROD0002", "NATIVES\\first.pdf"),
            ("PROD0004", "PROD0004", "NATIVES\\second.pdf"),
            ("PROD0005", "PROD0005", "..\\escape.pdf"),
            ("PROD0006", "PROD0006", "NATIVES\\absent.pdf"),
        ],
    )
    bad_dat = _dat_bytes(("CUSTODIAN", "TITLE"), [("Synthetic Custodian", "Sensitive private marker")])
    def _upload_volume(review, files):
        import time, uuid
        from pathlib import Path
        from tests.test_discovery_review_dat_opt import ACTOR
        from tests.test_intake_receipt_http import put, selection, upload

        media = {".pdf": "application/pdf", ".dat": "text/plain"}
        names = list(files)
        descriptors = [{"name": Path(n).name, "relative_path": n, "size": len(files[n]),
                        "media_type": media.get(Path(n).suffix.lower(), "text/plain")} for n in names]
        ordinals = list(range(len(names)))
        receipt = selection(review.client, review.matter.slug, descriptors, ordinals, key=uuid.uuid4().hex)
        session = upload(review.client, review.matter.slug, receipt, descriptors, ordinals)
        for name, item in zip(names, session["items"]):
            put(review.client, item, files[name], offset=0)
            assert review.client.post(item["finalize_url"]).status_code == 200
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            items = [review.bench.workspace.upload_item(review.matter.matter_id, ACTOR,
                session["upload_session_id"], item["upload_item_id"]) for item in session["items"]]
            jobs = [review.bench.workspace.ingest_job(review.matter.matter_id, item.document_id) for item in items]
            if all(job is not None and job.state in {"succeeded", "failed"} for job in jobs):
                assert all(job.state == "succeeded" for job in jobs), jobs
                break
            time.sleep(0.01)

    with opened_review(tmp_path / "runtime") as review:
        _upload_volume(review, {
            "Vol1/NATIVES/first.pdf": pdf_bytes(["Page one.", "Page two."]),
            "Vol1/NATIVES/second.pdf": pdf_bytes(["Page four."]),
            "Vol1/DATA/index.dat": dat_raw,
        })
        slug = review.matter.slug
        token = review.token("Vol1/DATA/index.dat")

        # DAT file appears in choices and computes exact missing Bates gap PROD0003.
        page = review.client.get(f"/matters/{slug}/production-summary", params={"load_file": token})
        assert page.status_code == 200, page.text
        assert HEADER in page.text
        assert "index.dat" in page.text
        assert "Missing Bates PROD0003" in page.text
        assert "Load file row 4 names no document in this production" in page.text
        assert "Load file row 5 names no document in this production" in page.text

        md = review.client.get(f"/matters/{slug}/production-summary/export", params={"format": "markdown", "load_file": token})
        js = review.client.get(f"/matters/{slug}/production-summary/export", params={"format": "json", "load_file": token})
        assert md.status_code == 200 and "Missing Bates PROD0003" in md.text
        assert js.status_code == 200
        data = js.json()
        assert data["load_file"]["sha256"] == hashlib.sha256(dat_raw).hexdigest()
        assert data["counts"]["missing"] == 1
        assert token in [c["token"] for c in data["choices"]]

        # Syntactically valid DAT lacking Bates/path headers fails closed with 400 and leaks no private row text.
        _upload_volume(review, {"Vol1/DATA/broken.dat": bad_dat})
        bad_resp = review.client.get(f"/matters/{slug}/production-summary", params={"load_file": review.token("Vol1/DATA/broken.dat")})
        assert bad_resp.status_code == 400
        assert "Sensitive private marker" not in bad_resp.text and "broken.dat" not in bad_resp.text


def test_resolve_dat_rows_enforces_root_ambiguity_and_control_chars(tmp_path):
    from types import SimpleNamespace

    def _doc(doc_id: str, rel: str):
        return SimpleNamespace(document_id=doc_id, version_id=f"v-{doc_id}", relative_path=rel,
                               media_type="application/pdf", page_count=1, state="ready")

    load = _doc("load", "Vol1/DATA/index.dat")
    vol1_doc = _doc("d1", "Vol1/NATIVES/a.pdf")
    vol2_doc = _doc("d2", "Vol2/NATIVES/a.pdf")

    # P1: Vol1 DAT cannot bind to Vol2 document or fall through to exact-path mapping.
    # P2: Control characters (e.g. trailing tab) are rejected before stripping.
    dat_file = tmp_path / "control_and_cross_root.dat"
    dat_file.write_bytes(_dat_bytes(
        ("BEGBATES", "ENDBATES", "NATIVELINK"),
        [
            ("SYN0001", "SYN0001", "Vol2/NATIVES/a.pdf"),
            ("SYN0002", "SYN0002", "Vol1/NATIVES/a.pdf\t"),
        ],
    ))
    parsed = summary.parse_dat_load_file(dat_file)
    resolved = summary._resolve_dat_rows((load, vol1_doc, vol2_doc), parsed, load)
    assert [row["path"] for row in resolved] == ["", ""]
    mapped = {doc.document_id: doc for doc in summary.map_documents((load, vol1_doc, vol2_doc), resolved, load.document_id)}
    assert mapped["d1"].labels == () and mapped["d2"].labels == ()

    # P2: Ambiguous matches inside the production root fail closed instead of falling through.
    nested_dup = _doc("d3", "Vol1/DATA/Vol1/NATIVES/a.pdf")
    with pytest.raises(ValueError, match="Ambiguous DAT document path"):
        summary._resolve_dat_rows(
            (load, vol1_doc, nested_dup),
            [{"path": "Vol1/NATIVES/a.pdf", "begin_bates": "SYN0003", "end_bates": "SYN0003"}],
            load,
        )

