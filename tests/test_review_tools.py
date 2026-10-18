from __future__ import annotations

from tests.fixture_write import fixture_write

import base64
import io
import re
import zipfile
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from case_intelligence.extended_extract import (
    MAX_TEXT_CONTAINER_BYTES,
    extract_delimited,
    extract_email,
)
from case_intelligence.generation import UnavailableGenerator
from case_intelligence.malware_scan import MalwareScanResult
from case_intelligence.pilot_uploads import PilotStore, UploadProblem
from case_intelligence.workbench import create_workbench_app


ACTOR = "development-taylor-morgan"


class CleanScanner:
    def scan(self, _path):
        return MalwareScanResult("clean", "synthetic")


class InfectedScanner:
    def scan(self, _path):
        return MalwareScanResult(
            "infected", "synthetic", signature="Synthetic.Test.Signature"
        )


def _matter(client: TestClient, name: str = "Synthetic review tools") -> str:
    response = client.post(
        "/matters",
        data={"name": name, "descriptor": "Generated review-tools acceptance"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    return response.headers["location"].split("/")[2]


def _xlsx() -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "xl/workbook.xml",
            '<?xml version="1.0"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="Calls" sheetId="1" r:id="rId1"/></sheets></workbook>',
        )
        archive.writestr(
            "xl/_rels/workbook.xml.rels",
            '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Target="worksheets/sheet1.xml" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet"/></Relationships>',
        )
        archive.writestr(
            "xl/worksheets/sheet1.xml",
            '<?xml version="1.0"?><worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>Name</t></is></c><c r="B1" t="inlineStr"><is><t>Duration</t></is></c></row><row r="2"><c r="A2" t="inlineStr"><is><t>Generated witness</t></is></c><c r="B2"><f>60*2</f><v>120</v></c></row></sheetData></worksheet>',
        )
    return output.getvalue()


def test_extended_sources_are_bounded_scanned_and_searchable(tmp_path):
    store = PilotStore(
        tmp_path / "sources",
        malware_scanner=CleanScanner(),
        malware_scan_mode="extended",
    )
    csv_document, _ = store.store_stream(
        "calls.csv",
        "text/csv",
        io.BytesIO(b"name,duration\nGenerated witness,120\n"),
    )
    assert csv_document.state == "ready"
    assert "Generated witness" in csv_document.parsed_units()[0].text

    email = (
        b"From: sender@example.test\r\n"
        b"To: reviewer@example.test\r\n"
        b"Subject: Generated evidence notice\r\n"
        b"Content-Type: text/plain; charset=utf-8\r\n\r\n"
        b"Officer Example described the generated meeting.\r\n"
    )
    email_document, _ = store.store_stream(
        "notice.eml", "message/rfc822", io.BytesIO(email)
    )
    assert email_document.state == "ready"
    assert "Subject: Generated evidence notice" in email_document.parsed_units()[0].text

    workbook_document, _ = store.store_stream(
        "calls.xlsx",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        io.BytesIO(_xlsx()),
    )
    assert workbook_document.state == "ready"
    workbook_text = workbook_document.parsed_units()[0].text
    assert "Generated witness" in workbook_text
    assert "=60*2 [cached: 120]" in workbook_text

    image_bytes = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
    )
    image_document, _ = store.store_stream(
        "scan.png", "image/png", io.BytesIO(image_bytes)
    )
    assert image_document.state == "ready"
    assert image_document.page_count == 1
    assert "no searchable text" in image_document.message


def test_text_container_size_is_rejected_before_read(tmp_path, monkeypatch):
    oversized = tmp_path / "oversized.eml"
    with oversized.open("wb") as handle:
        handle.truncate(MAX_TEXT_CONTAINER_BYTES + 1)

    def unexpected_read(_path):
        raise AssertionError("oversized container must not be loaded into memory")

    monkeypatch.setattr(type(oversized), "read_bytes", unexpected_read)
    with pytest.raises(ValueError, match="email exceeds"):
        extract_email(oversized)
    with pytest.raises(ValueError, match="spreadsheet exceeds"):
        extract_delimited(oversized, ",")


def test_extended_sources_fail_closed_and_detected_content_is_isolated(tmp_path):
    unavailable = PilotStore(tmp_path / "unavailable", malware_scan_mode="extended")
    with pytest.raises(UploadProblem) as exc:
        unavailable.store_stream(
            "table.csv", "text/csv", io.BytesIO(b"a,b\n1,2\n")
        )
    assert exc.value.status_code == 503
    assert not unavailable.documents

    infected = PilotStore(
        tmp_path / "infected",
        malware_scanner=InfectedScanner(),
        malware_scan_mode="extended",
    )
    with pytest.raises(UploadProblem) as exc:
        infected.store_stream(
            "scan.png", "image/png", io.BytesIO(b"synthetic unsafe payload")
        )
    assert exc.value.status_code == 422
    assert not infected.documents
    status = infected.malware_scan_status()
    assert status["quarantined"] == 1
    quarantined = tuple(infected.quarantine_root.iterdir())
    assert len(quarantined) == 1
    assert quarantined[0].suffix == ".blocked"

    ordinary, _ = unavailable.store_stream(
        "ordinary.txt", "text/plain", io.BytesIO(b"ordinary generated text\n")
    )
    assert ordinary.state == "ready"


def test_source_library_uses_sql_catalog_for_thousand_item_page(tmp_path):
    app = create_workbench_app(
        tmp_path / "runtime", generator=UnavailableGenerator(), auth_mode="test"
    )
    with TestClient(app) as client:
        slug = _matter(client, "Generated thousand-source catalog")
        bench = client.app.state.workbench
        matter = bench.matter(slug, ACTOR)
        store = bench.source_store(matter)
        store.register_linked_sources(
            source_location_id="generated-review-share",
            sources=tuple(
                SimpleNamespace(
                    relative_path=f"Batch {ordinal // 100:02d}/record-{ordinal:04d}.txt",
                    display_name=f"record-{ordinal:04d}.txt",
                    media_type="text/plain",
                    byte_size=64,
                    stable_device=1,
                    stable_inode=ordinal + 1,
                    stable_mtime_ns=1_700_000_000_000_000_000 + ordinal,
                )
                for ordinal in range(1_000)
            ),
        )
        catalog_count = bench.workspace.connection.execute(
            "SELECT COUNT(*) FROM workbench_source_catalog WHERE matter_id=?",
            (matter.matter_id,),
        ).fetchone()[0]
        assert catalog_count == 1_000

        def full_inventory_forbidden(_matter):
            raise AssertionError("source library must not build the complete manifest row set")

        bench._source_rows = full_inventory_forbidden
        page = client.get(f"/matters/{slug}/setup?view=list&page=7&page_size=50")
        assert page.status_code == 200
        assert "Page 7 of 20" in page.text
        assert page.text.count('name="selected"') == 50


def test_review_map_and_report_builder_preserve_citations(tmp_path):
    app = create_workbench_app(
        tmp_path / "runtime", generator=UnavailableGenerator(), auth_mode="test"
    )
    with TestClient(app) as client:
        slug = _matter(client, "Generated analysis and report")
        uploaded = client.post(
            f"/matters/{slug}/uploads",
            files=[
                (
                    "files",
                    (
                        "account-a.txt",
                        b"Officer Jane Smith was at Main Street on August 14, 2026.\n",
                        "text/plain",
                    ),
                ),
                (
                    "files",
                    (
                        "account-b.txt",
                        b"Officer Jane Smith was not at Main Street on August 14, 2026.\n",
                        "text/plain",
                    ),
                ),
            ],
        )
        assert uploaded.status_code == 200
        refreshed = client.post(
            f"/matters/{slug}/analysis/refresh", follow_redirects=False
        )
        assert refreshed.status_code == 303
        page = client.get(f"/matters/{slug}/analysis")
        assert page.status_code == 200
        assert "Officer Jane Smith" in page.text
        assert "Possible wording conflict" in page.text

        bench = client.app.state.workbench
        matter = bench.matter(slug, ACTOR)
        finding = next(
            item
            for item in bench.workspace.review_findings(matter.matter_id)
            if item.kind == "contradiction"
        )
        confirmed = client.post(
            f"/matters/{slug}/analysis/findings/{finding.finding_id}/status",
            data={"status": "confirmed"},
            follow_redirects=False,
        )
        assert confirmed.status_code == 303

        created = client.post(
            f"/matters/{slug}/reports",
            data={
                "title": "Generated review memorandum",
                "purpose": "Compare generated accounts.",
            },
            follow_redirects=False,
        )
        assert created.status_code == 303
        query = parse_qs(urlparse(created.headers["location"]).query)
        report_id = query["report"][0]
        added = client.post(
            f"/matters/{slug}/reports/{report_id}/from-finding/{finding.finding_id}",
            data={"expected_status": "draft"},
            follow_redirects=False,
        )
        assert added.status_code == 303
        manual = client.post(
            f"/matters/{slug}/reports/{report_id}/sections",
            data={"expected_status": "draft", "heading": "Reviewer conclusion", "body": "Pending human review."},
            follow_redirects=False,
        )
        assert manual.status_code == 303

        report_page = client.get(f"/matters/{slug}/reports?report={report_id}")
        assert report_page.status_code == 200
        assert "account-a.txt" in report_page.text
        assert "account-b.txt" in report_page.text
        assert "Reviewer conclusion" in report_page.text

        exported = client.get(
            f"/matters/{slug}/reports/{report_id}/export?format=markdown"
        )
        assert exported.status_code == 200
        text = exported.content.decode("utf-8")
        assert "Generated review memorandum" in text
        assert "account-a.txt" in text
        assert "Line 1" in text
        assert "Pending human review" in text

        foreign_marker = "Generated replacement passage from another source"
        with fixture_write(bench.workspace):
            bench.workspace.connection.execute(
                "UPDATE workbench_report_citation SET excerpt=? "
                "WHERE matter_id=? AND report_id=? AND kind='source'",
                (foreign_marker, matter.matter_id, report_id),
            )
        rejected = client.get(
            f"/matters/{slug}/reports/{report_id}/export?format=markdown"
        )
        assert rejected.status_code == 400
        assert "no longer resolves" in rejected.text
        assert foreign_marker not in rejected.text


def test_report_export_refuses_an_unresolvable_saved_media_clip(tmp_path):
    app = create_workbench_app(
        tmp_path / "runtime", generator=UnavailableGenerator(), auth_mode="test"
    )
    with TestClient(app) as client:
        slug = _matter(client, "Generated timestamp report")
        bench = client.app.state.workbench
        matter = bench.matter(slug, ACTOR)
        report = bench.workspace.create_report(
            matter.matter_id, ACTOR, "Timestamped review"
        )
        section = bench.workspace.add_report_section(
            matter.matter_id,
            report.report_id,
            ACTOR,
            expected_status=report.status,
            heading="Selected recording moment",
            body="Review the cited segment.",
            origin="media_clip",
            origin_id="media-clip-" + "a" * 32,
            citations=(
                {
                    "kind": "media_clip",
                    "document_id": "b" * 32,
                    "source_version_id": "c" * 32,
                    "source_name": "generated-recording.mp4",
                    "location": "00:12–00:18",
                    "media_clip_id": "media-clip-" + "a" * 32,
                    "start_ms": 12_000,
                    "end_ms": 18_000,
                },
            ),
        )
        citations = bench.workspace.report_citations(
            matter.matter_id, report.report_id, section.section_id
        )
        assert citations[0].start_ms == 12_000
        exported = client.get(
            f"/matters/{slug}/reports/{report.report_id}/export?format=markdown"
        )
        assert exported.status_code == 400
        assert "no longer resolves" in exported.text
        assert "generated-recording.mp4 — 00:12–00:18" not in exported.text
