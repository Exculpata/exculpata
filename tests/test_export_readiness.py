from tests.case_lifecycle_web_helpers import claim_bench_exit
"""Final-bundle preview reuses authoritative export checks and offers repair."""
import html
import io
import json
import re
import zipfile

import pytest
from fastapi.testclient import TestClient

from case_intelligence.work_product_exports import ExportProblem
from case_intelligence.workspace_store import WorkspaceProblem
from tests.test_reports_bundle import ACTOR, cited_report, saved_report, workspace  # noqa: F401


def test_ready_preview_uses_current_bundle_checks_and_does_not_export(workspace):
    client, bench, matter = workspace
    report, document = cited_report(bench, matter)
    response = client.get(f"/matters/{matter.slug}/export-readiness")
    assert response.status_code == 200
    assert "Ready to download" in response.text
    assert "Checked at" in response.text
    assert "checks again" in response.text
    assert "Original source files are not included in the bundle." in response.text
    assert "This work-product bundle is not a backup or a reopenable matter package." in response.text
    assert "Content-Disposition" not in response.headers
    assert bench.workspace.report(matter.matter_id, report.report_id).title == report.title
    assert bench.source_store(matter).get(document.document_id).version_id == document.version_id
    assert bench.matter_active_work_counts(matter.matter_id)["exports"] == 0
    assert not any(event.action == "work_product.export" and event.outcome == "success"
        for event in bench.workspace.audit_events(matter.matter_id))


def test_blocked_preview_identifies_reports_and_does_not_claim_later_checks(workspace):
    client, bench, matter = workspace
    saved_report(bench, matter, "Generated ready memo")
    report, document = cited_report(bench, matter)
    bench.remove_document(matter, bench.source_store(matter).action_token(document))
    response = client.get(f"/matters/{matter.slug}/export-readiness")
    assert response.status_code == 200
    assert "Export needs attention" in response.text
    assert report.title in response.text
    assert f"/matters/{matter.slug}/reports?report={report.report_id}" in response.text
    assert "Remaining bundle checks did not finish" in response.text
    assert "This work-product bundle is not a backup or a reopenable matter package." in response.text
    assert "Synthetic exact source passage." not in response.text
    assert client.get(f"/matters/{matter.slug}/export").status_code == 409
    assert bench.workspace.matter_lifecycle(matter.matter_id).state == "active"
    assert bench.matter_active_work_counts(matter.matter_id)["exports"] == 0


def test_download_revalidates_after_successful_preview(workspace):
    client, bench, matter = workspace
    _, document = cited_report(bench, matter)
    preview = client.get(f"/matters/{matter.slug}/export-readiness", headers={"Accept": "application/json"})
    assert preview.status_code == 200 and preview.json()["ready"] is True
    bench.remove_document(matter, bench.source_store(matter).action_token(document))
    assert client.get(f"/matters/{matter.slug}/export").status_code == 409


def test_multiple_report_repairs_preserve_other_work_and_enable_final_bundle(workspace):
    client, bench, matter = workspace
    reports = [cited_report(bench, matter) for _ in range(2)]
    for index, (report, _) in enumerate(reports):
        current = bench.workspace.report(matter.matter_id, report.report_id)
        bench.workspace.update_report(matter.matter_id, report.report_id, ACTOR,
            expected_updated_at=current.updated_at, title=f"Generated memo {index + 1}",
            purpose=current.purpose, status=current.status)
    for document_id in {document.document_id for _, document in reports}:
        store = bench.source_store(matter)
        bench.remove_document(matter, store.action_token(store.get(document_id)))
    route = f"/matters/{matter.slug}/export-readiness"
    result = client.get(route, headers={"Accept": "application/json"}).json()
    assert not result["ready"] and len(result["reports"]) == 2
    assert {item["title"] for item in result["reports"]} == {"Generated memo 1", "Generated memo 2"}
    for report, _ in reports:
        repair_url = next(item["url"] for item in result["reports"] if report.report_id in item["url"])
        page = client.get(repair_url)
        assert "This Report needs attention before export" in page.text
        assert "Source support" in page.text
        sections = bench.workspace.report_sections(matter.matter_id, report.report_id)
        stale = next(section for section in sections if section.heading == "Source support")
        removed = client.post(f"/matters/{matter.slug}/reports/{report.report_id}/sections/{stale.section_id}/delete",
            data={"expected_updated_at": stale.updated_at, "expected_status": "draft"}, follow_redirects=False)
        assert removed.status_code == 303
    assert client.get(route, headers={"Accept": "application/json"}).json()["ready"]
    exported = client.get(f"/matters/{matter.slug}/export")
    assert exported.status_code == 200
    with zipfile.ZipFile(io.BytesIO(exported.content)) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["report_count"] == 2
        for report in manifest["reports"]:
            assert "Preserve these edited words." in archive.read(report["markdown"]).decode()
        assert not manifest["original_source_files_included"]


@pytest.mark.parametrize("limit", ["MAX_FINAL_BUNDLE_REPORTS", "MAX_FINAL_BUNDLE_REPORT_ROWS",
    "MAX_FINAL_BUNDLE_REPORT_BYTES", "MAX_BUNDLE_UNCOMPRESSED_BYTES"])
def test_preview_uses_final_count_and_byte_limits_and_releases_lease(workspace, monkeypatch, limit):
    client, bench, matter = workspace
    saved_report(bench, matter)
    monkeypatch.setattr("case_intelligence.workbench." + limit, 0)
    response = client.get(f"/matters/{matter.slug}/export-readiness", headers={"Accept": "application/json"})
    assert response.status_code == 200 and not response.json()["ready"]
    final = client.get(f"/matters/{matter.slug}/export")
    assert final.status_code == 409 and final.text == response.json()["problem"]
    assert bench.matter_active_work_counts(matter.matter_id)["exports"] == 0


def test_preview_exercises_packaging_and_never_caches_final_authority(workspace, monkeypatch):
    client, bench, matter = workspace
    saved_report(bench, matter)
    calls = []
    def refused(*_args, **_kwargs):
        calls.append(True)
        raise ExportProblem("Generated packaging failure")
    monkeypatch.setattr("case_intelligence.workbench.export_matter_bundle", refused)
    response = client.get(f"/matters/{matter.slug}/export-readiness", headers={"Accept": "application/json"})
    assert response.status_code == 200 and not response.json()["ready"]
    assert response.json()["problem"] == "Generated packaging failure"
    assert client.get(f"/matters/{matter.slug}/export").status_code == 409
    assert len(calls) == 2
    assert bench.matter_active_work_counts(matter.matter_id)["exports"] == 0


def test_preview_refuses_concurrent_closure_and_preserves_report_revision(workspace, monkeypatch):
    client, bench, matter = workspace
    report, _ = cited_report(bench, matter)
    before = bench.workspace.report(matter.matter_id, report.report_id)
    original = bench.export_report_work_products
    checked = []
    def inspect(*args, **kwargs):
        assert bench.matter_active_work_counts(matter.matter_id)["exports"] == 1
        with pytest.raises(WorkspaceProblem):
            bench.begin_matter_purge(matter.slug, ACTOR, matter.display_name)
        checked.append(True)
        return original(*args, **kwargs)
    monkeypatch.setattr(bench, "export_report_work_products", inspect)
    assert client.get(f"/matters/{matter.slug}/export-readiness").status_code == 200
    assert checked == [True]
    assert bench.workspace.report(matter.matter_id, report.report_id) == before
    assert bench.workspace.matter_lifecycle(matter.matter_id).state == "active"
    assert bench.matter_active_work_counts(matter.matter_id)["exports"] == 0


@pytest.mark.parametrize("cited", [False, True])
def test_failed_close_preview_never_reopens_quarantined_sources(workspace, monkeypatch, cited):
    client, bench, matter = workspace
    (cited_report if cited else saved_report)(bench, matter)
    bench._postgres_projection_configured = True
    bench.postgres_connection = None
    bench.postgres_ready = False
    prepared, lifecycle = claim_bench_exit(bench, matter, ACTOR)
    with pytest.raises(WorkspaceProblem, match="derived search data"):
        bench.execute_matter_purge(prepared, lifecycle)
    def forbidden(*_args, **_kwargs):
        pytest.fail("Preview must not reopen quarantined source state")
    monkeypatch.setattr(bench, "source_store", forbidden)
    response = client.get(f"/matters/{matter.slug}/export-readiness")
    assert response.status_code == 200
    assert ("Export needs attention" if cited else "Ready to download") in response.text
    assert "This work-product bundle is not a backup or a reopenable matter package." in response.text
    assert f'href="/matters/{matter.slug}/close">Return to close matter</a>' in response.text
    report_links = re.findall(r'<a href="([^"]+)">Open ', response.text)
    for link in report_links:
        assert client.get(html.unescape(link)).status_code == 200
    if cited:
        assert not report_links
        result = client.get(f"/matters/{matter.slug}/export-readiness", headers={"Accept": "application/json"}).json()
        assert result["reports"] and all(not item["url"] for item in result["reports"])
        assert "Reports cannot be edited" in response.text
    assert client.get(f"/matters/{matter.slug}/close").status_code == 200
    assert bench.workspace.matter_lifecycle(matter.matter_id).state == "purge_failed"
    assert bench.matter_active_work_counts(matter.matter_id)["exports"] == 0


def test_foreign_owner_preview_does_not_disclose_report_names(workspace):
    client, bench, _ = workspace
    actor = "generated-foreign-owner"
    bench.workspace.upsert_principal("test", actor, "Generated other owner", actor, preferred_principal_id=actor)
    foreign = bench.create_matter("Generated foreign export boundary", "", actor)
    bench.workspace.create_report(foreign.matter_id, actor, "Foreign report canary")
    for suffix in ("export-readiness", "export"):
        denied = client.get(f"/matters/{foreign.slug}/{suffix}")
        assert denied.status_code == 404 and "Foreign report canary" not in denied.text
    assert bench.matter_active_work_counts(foreign.matter_id)["exports"] == 0


def test_revocation_during_preview_withholds_result_and_releases_lease(tmp_path, monkeypatch):
    from tests.test_matter_management import OWNER, OTHER, _app, _create_matter, _csrf, _headers, _principal_id
    app = _app(tmp_path)
    with TestClient(app, base_url="https://testserver") as client:
        token = _csrf(client.get("/matters/new", headers=_headers(OWNER)).text)
        slug = _create_matter(client, principal=OWNER, csrf_token=token, name="Generated preview revocation")
        owner = _principal_id(client, OWNER)
        client.get("/matters/new", headers=_headers(OTHER))
        member = _principal_id(client, OTHER)
        bench = app.state.workbench
        matter = bench.workspace.get_matter(slug, owner)
        bench.workspace.add_member(matter.matter_id, member, owner)
        bench.workspace.create_report(matter.matter_id, owner, "Generated revoked-reader Report")
        import case_intelligence.workbench as module
        original = module.export_matter_bundle
        def revoke(*args, **kwargs):
            artifact = original(*args, **kwargs)
            bench.workspace.revoke_member(matter.matter_id, member, owner)
            return artifact
        monkeypatch.setattr(module, "export_matter_bundle", revoke)
        response = client.get(f"/matters/{slug}/export-readiness", headers=_headers(OTHER))
        assert response.status_code == 404 and "Generated revoked-reader Report" not in response.text
        assert bench.matter_active_work_counts(matter.matter_id)["exports"] == 0
