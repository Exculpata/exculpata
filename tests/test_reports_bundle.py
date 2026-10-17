
from case_intelligence.legacy_names import (
    LEGACY_STEM,
)
from tests.case_lifecycle_web_helpers import claim_bench_exit
"""Synthetic regressions for preserving saved Reports in complete exports."""
import io
import json
import sqlite3
import zipfile

import pytest
from fastapi.testclient import TestClient

from case_intelligence.generation import UnavailableGenerator
from case_intelligence.workbench import create_workbench_app
from case_intelligence.workspace_store import WorkspaceProblem

ACTOR = "development-taylor-morgan"


@pytest.fixture
def workspace(tmp_path):
    app = create_workbench_app(
        tmp_path / "runtime", generator=UnavailableGenerator(), auth_mode="test"
    )
    with TestClient(app) as client:
        response = client.post("/matters", data={"name": "Synthetic report bundle"},
                               follow_redirects=False)
        slug = response.headers["location"].split("/")[2]
        bench = app.state.workbench
        yield client, bench, bench.matter(slug, ACTOR)


def saved_report(bench, matter, title="Review memo", status="draft"):
    store = bench.workspace
    report = store.create_report(matter.matter_id, ACTOR, title, "Review purpose")
    store.add_report_section(matter.matter_id, report.report_id, ACTOR,
                             expected_status=report.status, heading="Human edits", body="Preserve these edited words.")
    return store.update_report(matter.matter_id, report.report_id, ACTOR,
                               expected_updated_at=store.report(matter.matter_id, report.report_id).updated_at,
                               title=title, purpose="Review purpose", status=status)


def test_complete_bundle_preserves_all_saved_reports(workspace):
    client, bench, matter = workspace
    for title, status in (("Review memo", "draft"), ("Review memo", "final"),
                          ("REVIEW MEMO", "draft"), ("../Odd/title", "final")):
        saved_report(bench, matter, title, status)
    bench.workspace.create_report(matter.matter_id, ACTOR, "Empty report")
    response = client.get(f"/matters/{matter.slug}/export")
    assert response.status_code == 200
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["report_count"] == 5
        assert len(manifest["reports"]) == 5
        assert len(set(name.casefold() for name in archive.namelist())) == len(archive.namelist())
        for item in manifest["reports"]:
            markdown = archive.read(item["markdown"]).decode()
            assert item["title"] in markdown
            assert f'{item["status"].title()} work product' in markdown
            assert item["markdown"].startswith("reports/")
            assert ".." not in item["markdown"].split("/")
            with zipfile.ZipFile(io.BytesIO(archive.read(item["docx"]))) as word:
                xml = word.read("word/document.xml").decode()
            if item["section_count"]:
                assert "Preserve these edited words." in markdown
                assert "Preserve these edited words." in xml
                assert "Review purpose" in markdown
        assert not manifest["original_source_files_included"]


def test_empty_bundle_has_empty_report_inventory(workspace):
    client, _, matter = workspace
    with zipfile.ZipFile(io.BytesIO(client.get(f"/matters/{matter.slug}/export").content)) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["report_count"] == 0
        assert manifest["reports"] == []


def test_report_limit_returns_no_complete_bundle_and_releases_lease(workspace, monkeypatch):
    client, bench, matter = workspace
    saved_report(bench, matter)
    monkeypatch.setattr("case_intelligence.workbench.MAX_FINAL_BUNDLE_REPORTS", 0)
    response = client.get(f"/matters/{matter.slug}/export")
    assert response.status_code == 409
    assert "No complete bundle" in response.text
    assert "Report" in response.text
    monkeypatch.setattr("case_intelligence.workbench.MAX_FINAL_BUNDLE_REPORTS", 500)
    assert client.get(f"/matters/{matter.slug}/export").status_code == 200


def cited_report(bench, matter):
    report = saved_report(bench, matter)
    document, _ = bench.source_store(matter).store_stream(
        "generated-source.txt", "text/plain", io.BytesIO(b"Synthetic exact source passage.")
    )
    unit = document.parsed_units()[0]
    candidate = bench._candidate(matter, document, unit, 1)
    bench.workspace.add_report_section(
        matter.matter_id, report.report_id, ACTOR, expected_status=report.status, heading="Source support", body="Edited finding.",
        citations=({"kind": "source", "document_id": document.document_id,
                    "source_version_id": document.version_id, "source_name": document.display_name,
                    "location": candidate.citation, "support_token": bench._support_token(candidate),
                    "excerpt": unit.text},),
    )
    return report, document


def test_cited_report_matches_individual_export_and_opens_exact_support(workspace):
    client, bench, matter = workspace
    report, document = cited_report(bench, matter)
    section = bench.workspace.report_sections(matter.matter_id, report.report_id)[-1]
    citation = bench.workspace.report_citations(matter.matter_id, report.report_id, section.section_id)[0]
    resolved, units, index = bench._find_support(matter, citation.support_token)
    assert resolved.version_id == document.version_id
    assert units[index].text == citation.excerpt
    individual = client.get(f"/matters/{matter.slug}/reports/{report.report_id}/export?format=markdown")
    response = client.get(f"/matters/{matter.slug}/export")
    assert response.status_code == individual.status_code == 200
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        item = json.loads(archive.read("manifest.json"))["reports"][0]
        bundled = archive.read(item["markdown"]).decode()
    # Only the export time differs; report text, source appendix and order agree.
    def without_time(text):
        return [line for line in text.splitlines() if "work product exported from" not in line]
    assert without_time(bundled) == without_time(individual.text)
    assert "generated-source.txt — Line 1" in bundled
    assert "Synthetic exact source passage." in bundled


def test_bundle_resolves_report_citations_once_for_both_formats(workspace, monkeypatch):
    client, bench, matter = workspace
    cited_report(bench, matter)
    original_validate = bench._assert_current_report_section_citations
    resolved = []
    def count_resolution(*args, **kwargs):
        resolved.append(True)
        return original_validate(*args, **kwargs)
    monkeypatch.setattr(bench, "_assert_current_report_section_citations", count_resolution)
    response = client.get(f"/matters/{matter.slug}/export")
    assert response.status_code == 200
    assert len(resolved) == 1
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        report = json.loads(archive.read("manifest.json"))["reports"][0]
        assert archive.read(report["markdown"])
        assert archive.read(report["docx"])


@pytest.mark.parametrize("column,value", [
    ("excerpt", "FOREIGN CONTENT CANARY"), ("source_version_id", "f" * 32),
    ("document_id", "e" * 32), ("location", "Line 99"), ("support_token", "d" * 40),
])
def test_bad_citation_never_returns_a_complete_bundle(workspace, column, value):
    client, bench, matter = workspace
    report, _ = cited_report(bench, matter)
    with bench.workspace.connection:
        bench.workspace.connection.execute(
            f"UPDATE workbench_report_citation SET {column}=? WHERE report_id=?",
            (value, report.report_id),
        )
    response = client.get(f"/matters/{matter.slug}/export")
    assert response.status_code == 409
    assert "no longer resolves" in response.text
    assert "FOREIGN CONTENT CANARY" not in response.text
    assert bench.matter_active_work_counts(matter.matter_id)["exports"] == 0
    assert not any(event.action == "work_product.export" and event.outcome == "success"
                   for event in bench.workspace.audit_events(matter.matter_id))


@pytest.mark.parametrize("limit", ["MAX_FINAL_BUNDLE_REPORT_ROWS", "MAX_FINAL_BUNDLE_REPORT_BYTES",
                                   "MAX_BUNDLE_UNCOMPRESSED_BYTES"])
def test_report_row_and_byte_limits_fail_without_omission(workspace, monkeypatch, limit):
    client, bench, matter = workspace
    saved_report(bench, matter)
    monkeypatch.setattr("case_intelligence.workbench." + limit, 1)
    response = client.get(f"/matters/{matter.slug}/export")
    assert response.status_code == 409
    assert "No complete bundle" in response.text
    assert bench.matter_active_work_counts(matter.matter_id)["exports"] == 0


def test_snapshot_does_not_mix_concurrent_report_edits(workspace, monkeypatch):
    _, bench, matter = workspace
    report = saved_report(bench, matter)
    original_sections = bench.workspace.report_sections
    def edit_between_reads(matter_id, report_id):
        with sqlite3.connect(bench.workspace.path) as other:
            other.create_function(f"{LEGACY_STEM}_case_content_cleanup", 1, lambda _matter_id: 0)
            other.execute("UPDATE workbench_report SET title='Concurrent title' WHERE report_id=?",
                          (report_id,))
            other.execute("UPDATE workbench_report_section SET body='Concurrent body' WHERE report_id=?",
                          (report_id,))
        return original_sections(matter_id, report_id)
    monkeypatch.setattr(bench.workspace, "report_sections", edit_between_reads)
    snapshot = bench.workspace.reports_for_final_bundle(
        matter.matter_id, ACTOR, maximum=500, maximum_rows=10_000, maximum_bytes=32*1024*1024)
    assert snapshot[0][0].title == report.title
    assert snapshot[0][1][0][0].body == "Preserve these edited words."
    assert bench.workspace.report(matter.matter_id, report.report_id).title == "Concurrent title"
    assert not bench.workspace.connection.in_transaction


@pytest.mark.parametrize("cited", [False, True])
def test_failed_close_recovery_never_reopens_quarantined_sources(workspace, monkeypatch, cited):
    client, bench, matter = workspace
    (cited_report if cited else saved_report)(bench, matter)
    bench._postgres_projection_configured = True
    bench.postgres_connection = None
    bench.postgres_ready = False
    prepared, lifecycle = claim_bench_exit(bench, matter, ACTOR)
    with pytest.raises(WorkspaceProblem, match="derived search data"):
        bench.execute_matter_purge(prepared, lifecycle)
    assert bench.workspace.matter_lifecycle(matter.matter_id).state == "purge_failed"
    def forbidden(*args, **kwargs):
        pytest.fail("Final export must not reopen quarantined sources")
    monkeypatch.setattr(bench, "source_store", forbidden)
    response = client.get(f"/matters/{matter.slug}/export")
    assert response.status_code == (409 if cited else 200)
    if cited:
        assert "restore source access" in response.text
    else:
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            assert json.loads(archive.read("manifest.json"))["report_count"] == 1
    assert matter.matter_id not in bench._stores
    assert bench.matter_active_work_counts(matter.matter_id)["exports"] == 0


def test_bundle_keeps_neighbor_report_out_and_refuses_close_during_render(workspace, monkeypatch):
    client, bench, matter = workspace
    saved_report(bench, matter)
    response = client.post("/matters", data={"name": "Neighbor synthetic matter"}, follow_redirects=False)
    neighbor = bench.matter(response.headers["location"].split("/")[2], ACTOR)
    saved_report(bench, neighbor, "FOREIGN REPORT CANARY")
    original_export = bench.export_report_work_products
    attempts = []
    def render_with_close_attempt(*args, **kwargs):
        with pytest.raises(WorkspaceProblem):
            bench.begin_matter_purge(matter.slug, ACTOR, matter.display_name)
        attempts.append(True)
        return original_export(*args, **kwargs)
    monkeypatch.setattr(bench, "export_report_work_products", render_with_close_attempt)
    response = client.get(f"/matters/{matter.slug}/export")
    assert response.status_code == 200 and len(attempts) == 1
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        assert b"FOREIGN REPORT CANARY" not in archive.read("manifest.json")
        assert json.loads(archive.read("manifest.json"))["report_count"] == 1
    assert bench.workspace.matter_lifecycle(matter.matter_id).state == "active"
    assert bench.matter_active_work_counts(matter.matter_id)["exports"] == 0


def test_orphaned_report_citation_refuses_complete_bundle(workspace):
    client, bench, matter = workspace
    report, _ = cited_report(bench, matter)
    other_report = saved_report(bench, matter, "Different saved Report")
    other_section = bench.workspace.report_sections(matter.matter_id, other_report.report_id)[0]
    with bench.workspace.connection:
        bench.workspace.connection.execute(
            "UPDATE workbench_report_citation SET section_id=? WHERE report_id=?",
            (other_section.section_id, report.report_id),
        )
    response = client.get(f"/matters/{matter.slug}/export")
    assert response.status_code == 409
    assert "inconsistent" in response.text


def test_report_bundle_survives_workspace_restart(tmp_path):
    runtime = tmp_path / "runtime"
    def app():
        return create_workbench_app(runtime, generator=UnavailableGenerator(), auth_mode="test")
    with TestClient(app()) as client:
        response = client.post("/matters", data={"name": "Synthetic restart"}, follow_redirects=False)
        slug = response.headers["location"].split("/")[2]
        bench = client.app.state.workbench
        matter = bench.matter(slug, ACTOR)
        cited_report(bench, matter)
    with TestClient(app()) as client:
        response = client.get(f"/matters/{slug}/export")
        assert response.status_code == 200
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            item = json.loads(archive.read("manifest.json"))["reports"][0]
            assert "Synthetic exact source passage." in archive.read(item["markdown"]).decode()


def test_timestamped_clip_and_transcript_support_survive_bundle(tmp_path):
    from tests.test_matter_media_workflow import ImmediateMediaProcessor, _matter, _upload_and_wait
    app = create_workbench_app(
        tmp_path / "runtime", generator=UnavailableGenerator(), auth_mode="test",
        media_processor=ImmediateMediaProcessor(), media_poll_seconds=0.01,
    )
    with TestClient(app) as client:
        slug = _matter(client, "Synthetic timestamp Report bundle")
        document, _ = _upload_and_wait(client, slug)
        bench = app.state.workbench
        matter = bench.matter(slug, ACTOR)
        clip = bench.workspace.create_media_clip(
            matter.matter_id, document.document_id, document.version_id,
            title="Selected moment", start_ms=0, end_ms=2000, actor_id=ACTOR,
        )
        report = saved_report(bench, matter)
        bench.add_media_clip_to_report(matter, ACTOR, report.report_id, clip.clip_id, expected_status=report.status)
        unit = document.parsed_units()[0]
        candidate = bench._candidate(matter, document, unit, 1)
        bench.workspace.add_report_section(
            matter.matter_id, report.report_id, ACTOR, expected_status=report.status, heading="Transcript support", body="Spoken passage.",
            citations=({"kind": "transcript", "document_id": document.document_id,
                        "source_version_id": document.version_id, "source_name": document.display_name,
                        "location": candidate.citation, "support_token": bench._support_token(candidate),
                        "excerpt": unit.text},),
        )
        response = client.get(f"/matters/{slug}/export")
        assert response.status_code == 200
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            item = json.loads(archive.read("manifest.json"))["reports"][0]
            text = archive.read(item["markdown"]).decode()
            assert "Interview.wav — 00:00–00:02" in text
            assert unit.text in text
        with bench.workspace.connection:
            bench.workspace.connection.execute(
                "UPDATE workbench_report_citation SET source_name='FOREIGN CLIP CANARY' WHERE report_id=? AND kind='media_clip'",
                (report.report_id,),
            )
        rejected = client.get(f"/matters/{slug}/export")
        assert rejected.status_code == 409
        assert "FOREIGN CLIP CANARY" not in rejected.text
