"""Synthetic browser-facing report compilation and document lifecycle."""
from __future__ import annotations

from tests.fixture_write import fixture_write

import io
import threading
import time
import zipfile
from urllib.parse import parse_qs, urlparse

import pytest

from tests.case_lifecycle_web_helpers import claim_bench_exit
from fastapi.testclient import TestClient

from case_intelligence.generation import GroundedGenerationService
from tests.test_report_compilation import SourceEchoClient
from tests.test_report_review_basis import ACTOR, saved_research, workspace
from tests.test_matter_management import OWNER, OTHER, _app, _create_matter, _csrf, _headers


def queue(client, matter, selections, *, kind="timeline", topic="", key="synthetic-report-request"):
    response = client.post(f"/matters/{matter.slug}/reports/compile", data={
        "kind": kind, "topic": topic, "selection": selections, "request_key": key,
    }, follow_redirects=False)
    assert response.status_code == 303, response.text
    return parse_qs(urlparse(response.headers["location"]).query)["job"][0]


def finished(client, matter, job_id):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        response = client.get(f"/matters/{matter.slug}/reports/compile/{job_id}/status")
        assert response.status_code == 200
        state = response.json()
        if state["terminal"]:
            return state
        time.sleep(.02)
    raise AssertionError("Synthetic report did not finish")


@pytest.mark.parametrize("kind", ["timeline", "entities", "topic"])
def test_compile_saved_research_to_readable_editable_exportable_document(workspace, kind):
    client, bench, matter = workspace
    bench.generator = GroundedGenerationService(SourceEchoClient())
    research, document = saved_research(bench, matter)
    selected = f"research:{research.job_id}"
    composer = client.get(f"/matters/{matter.slug}/reports/new?from={selected}")
    assert composer.status_code == 200 and 'value="' + selected + '" checked' in composer.text
    job_id = queue(client, matter, [selected], kind=kind, topic="bicycle arrival", key="synthetic-report-" + kind)
    result = finished(client, matter, job_id)
    assert result["state"] == "succeeded", result
    page = client.get(result["result_url"])
    assert page.status_code == 200 and 'class="report-reading-page"' in page.text
    assert 'textarea name="body"' not in page.text
    report = bench.workspace.reports(matter.matter_id, ACTOR)[0]
    section = next(item for item in bench.workspace.report_sections(matter.matter_id, report.report_id)
                   if bench.workspace.report_citations(matter.matter_id, report.report_id, item.section_id))
    edit = client.get(result["result_url"] + "&edit=true")
    assert edit.status_code == 200 and 'name="body"' in edit.text
    marker = "Human reviewer requests the missing gate log."
    response = client.post(f"/matters/{matter.slug}/reports/{report.report_id}/sections/{section.section_id}", data={
        "heading": section.heading, "body": section.body.split("\n\nReview basis:\n", 1)[0] + "\n" + marker,
        "expected_updated_at": section.updated_at, "expected_status": report.status,
    }, follow_redirects=False)
    assert response.status_code == 303
    retained = next(item for item in bench.workspace.report_sections(matter.matter_id, report.report_id) if item.section_id == section.section_id)
    assert retained.body.split("\n\nReview basis:\n", 1)[1] == section.body.split("\n\nReview basis:\n", 1)[1]
    for fmt in ("markdown", "docx"):
        exported = client.get(f"/matters/{matter.slug}/reports/{report.report_id}/export?format={fmt}")
        assert exported.status_code == 200, exported.text[:100]
        if fmt == "docx":
            with zipfile.ZipFile(io.BytesIO(exported.content)) as archive:
                text = archive.read("word/document.xml").decode()
        else:
            text = exported.text
        assert marker in text and document.display_name in text and "Compilation coverage" in text
    assert queue(client, matter, [selected], kind=kind, topic="bicycle arrival", key="synthetic-report-" + kind) == job_id
    assert len(bench.workspace.reports(matter.matter_id, ACTOR)) == 1


def test_changed_selected_work_during_generation_fails_without_orphan_and_retries(workspace):
    client, bench, matter = workspace
    entered, release = threading.Event(), threading.Event()
    class BlockingClient(SourceEchoClient):
        def generate(self, **kwargs):
            entered.set()
            assert release.wait(8)
            return super().generate(**kwargs)
    bench.generator = GroundedGenerationService(BlockingClient())
    research, _ = saved_research(bench, matter)
    note, _ = bench.workspace.create_notebook_item(matter.matter_id, ACTOR,
        item_type="event", status="confirmed", title="Bicycle arrival", body="Reviewer observed the arrival.")
    job_id = queue(client, matter, ["notes:active", f"research:{research.job_id}"])
    try:
        assert entered.wait(5)
        bench.workspace.update_notebook_item(matter.matter_id, ACTOR, note.item_id,
            expected_updated_at=note.updated_at, item_type="event", status="disputed",
            title=note.title, body="The time of arrival is now disputed.")
    finally:
        release.set()
    result = finished(client, matter, job_id)
    assert result["state"] == "failed" and "changed" in result["message"]
    assert bench.workspace.reports(matter.matter_id, ACTOR) == ()
    retry = client.post(f"/matters/{matter.slug}/reports/compile/{job_id}/retry", follow_redirects=False)
    assert retry.status_code == 303
    assert finished(client, matter, job_id)["state"] == "succeeded"
    assert len(bench.workspace.reports(matter.matter_id, ACTOR)) == 1


def test_queued_cancel_and_missing_material_never_create_empty_reports(workspace):
    client, bench, matter = workspace
    bench.report_compilation.coordinator.close()
    job_id = queue(client, matter, ["notes:active"])
    response = client.post(f"/matters/{matter.slug}/reports/compile/{job_id}/cancel", follow_redirects=False)
    assert response.status_code == 303
    cancelled = finished(client, matter, job_id)
    assert cancelled["state"] == "cancelled" and cancelled["message"] == "Compilation cancelled."
    assert not bench.workspace.reports(matter.matter_id, ACTOR)
    assert client.get(f"/matters/{matter.slug}/reports/new").status_code == 200


def test_compilation_routes_require_membership_and_csrf(tmp_path, monkeypatch):
    monkeypatch.setenv("CASE_INTELLIGENCE_STORAGE_RESERVE_GIB", "0")
    app = _app(tmp_path)
    with TestClient(app, base_url="https://testserver") as client:
        token = _csrf(client.get('/matters/new', headers=_headers(OWNER)).text)
        slug = _create_matter(client, principal=OWNER, csrf_token=token, name="Synthetic private report")
        data = {"kind": "timeline", "selection": ["notes:active"], "request_key": "synthetic-secure-request"}
        path = f"/matters/{slug}/reports/compile"
        assert client.post(path, data={**data, "csrf_token": "wrong"}, headers=_headers(OWNER)).status_code == 403
        assert client.get(f"/matters/{slug}/reports/new", headers=_headers(OTHER)).status_code == 404
        assert client.post(path, data={**data, "csrf_token": token}, headers=_headers(OTHER)).status_code in {403, 404}
        assert not app.state.workbench.workspace.connection.execute("SELECT 1 FROM workbench_report_compilation_job").fetchone()


@pytest.mark.parametrize("repeats", [700, 900])
def test_canonical_source_limit_preserves_bounded_support_and_rejects_oversize(workspace, repeats):
    client, bench, matter = workspace
    full_text = "A synthetic account " + "bicycle " * repeats + "END-OF-CANONICAL-SOURCE."
    document, _ = bench.source_store(matter).store_stream('synthetic-long-source.txt', 'text/plain', io.BytesIO(full_text.encode()))
    unit = document.parsed_units()[0]
    candidate = bench._candidate(matter, document, unit, 1)
    reference = bench._workflow_citation_payload(bench._citation(matter, candidate))
    reference.update(chunk_id=candidate.chunk_id, unit_number=unit.number,
        excerpt_digest=unit.excerpt_digest, excerpt=unit.text[:6000])
    note, _ = bench.workspace.create_notebook_item(matter.matter_id, ACTOR, item_type='event',
        status='confirmed', title='Long account retained', body='Human reviewer flags the long account for comparison.', references=[reference])
    result = finished(client, matter, queue(client, matter, [f'note:{note.item_id}'], key='synthetic-long-canonical'))
    if len(unit.text) > 6000:
        assert result["state"] == "failed" and "6,000-character" in result["message"]
        assert not bench.workspace.reports(matter.matter_id, ACTOR)
        return
    assert result['state'] == 'succeeded', result
    report = bench.workspace.reports(matter.matter_id, ACTOR)[0]
    citations = [citation for section in bench.workspace.report_sections(matter.matter_id, report.report_id)
                 for citation in bench.workspace.report_citations(matter.matter_id, report.report_id, section.section_id)]
    assert any(citation.excerpt == unit.text for citation in citations)
    for fmt in ('markdown', 'docx'):
        response = client.get(f'/matters/{matter.slug}/reports/{report.report_id}/export?format={fmt}')
        assert response.status_code == 200, response.text[:100]
        if fmt == 'docx':
            with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
                text = archive.read('word/document.xml').decode()
        else:
            text = response.text
        assert 'END-OF-CANONICAL-SOURCE.' in text


def test_deleted_compiled_result_can_be_recreated_without_changing_original_request(workspace):
    client, bench, matter = workspace
    note, _ = bench.workspace.create_notebook_item(matter.matter_id, ACTOR,
        item_type="event", status="confirmed", title="Synthetic arrival", body="Reviewer records an unresolved arrival.")
    job_id = queue(client, matter, [f"note:{note.item_id}"], key="synthetic-delete-recreate")
    assert finished(client, matter, job_id)["state"] == "succeeded"
    original = bench.workspace.reports(matter.matter_id, ACTOR)[0]
    bench.workspace.delete_report(matter.matter_id, original.report_id, ACTOR, expected_updated_at=original.updated_at)
    progress = client.get(f"/matters/{matter.slug}/reports/new?job={job_id}")
    assert "This draft was deleted" in progress.text
    listing = client.get(f"/matters/{matter.slug}/reports")
    assert f"/reports/new?job={job_id}" in listing.text and "This draft was deleted" in listing.text
    status = finished(client, matter, job_id)
    assert status["result_url"].endswith(f"/reports/new?job={job_id}")
    assert "This draft was deleted" in status["message"]
    response = client.post(f"/matters/{matter.slug}/reports/compile/{job_id}/retry", follow_redirects=False)
    assert response.status_code == 303
    assert finished(client, matter, job_id)["state"] == "succeeded"
    reports = bench.workspace.reports(matter.matter_id, ACTOR)
    assert len(reports) == 1 and reports[0].report_id != original.report_id


def test_source_delimiters_remain_prose_and_compilation_basis_survives_restore(workspace, tmp_path):
    import sqlite3
    from case_intelligence.workspace_store import WorkspaceStore
    client, bench, matter = workspace
    marker = "\n\nReview basis:\n"
    user_text = "Synthetic visible opening." + marker + "This is user text and must stay editable."
    basis = "Compiler-authored synthetic provenance."
    report = bench.workspace.create_report_from_sections(matter.matter_id, ACTOR, "Literal marker", "",
        origin_id="report-compilation-" + "d" * 32,
        sections=[{"heading": "Human note", "body": user_text + marker + basis, "compilation_basis": basis}])
    section = bench.workspace.report_sections(matter.matter_id, report.report_id)[0]
    assert section.prose == user_text and section.compilation_basis == basis
    page = client.get(f"/matters/{matter.slug}/reports?report={report.report_id}")
    assert 'class="report-reading-text">' + user_text in page.text
    edit = client.get(f"/matters/{matter.slug}/reports?report={report.report_id}&edit=true")
    assert user_text + "</textarea>" in edit.text
    replacement = "The user removed the confusing quoted marker."
    bench.workspace.update_report_section(matter.matter_id, report.report_id, section.section_id, ACTOR,
        expected_updated_at=section.updated_at, expected_status=report.status, heading=section.heading, body=replacement)
    updated = bench.workspace.report_sections(matter.matter_id, report.report_id)[0]
    assert updated.prose == replacement and updated.compilation_basis == basis
    destination = tmp_path / "clean-basis-restore.sqlite"
    with sqlite3.connect(destination) as restored:
        bench.workspace.connection.backup(restored)
    with_store = WorkspaceStore(destination)
    try:
        retained = with_store.report_sections(matter.matter_id, report.report_id)[0]
        assert retained == updated
        assert with_store.connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        with_store.close()
    # Startup is repeatable, and legacy text is never heuristically split.
    with sqlite3.connect(destination) as legacy:
        # fixture-write: Reconstruct the isolated backup schema after closing its restored WorkspaceStore.
        legacy.execute("ALTER TABLE workbench_report_section DROP COLUMN compilation_basis")
    legacy_store = WorkspaceStore(destination)
    try:
        legacy_section = legacy_store.report_sections(matter.matter_id, report.report_id)[0]
        assert not legacy_section.compilation_basis and legacy_section.prose == updated.body
    finally:
        legacy_store.close()


def test_compilation_basis_migration_mirrors_operator_marker():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    assert (root / "migrations/sqlite/0029_report_compilation_basis.sql").read_bytes() == (root / "src/case_intelligence/migrations/sqlite/0029_report_compilation_basis.sql").read_bytes()


def test_exact_copied_compilation_suffix_stays_literal_prose(workspace):
    client, bench, matter = workspace
    basis = "Saved synthetic provenance."
    suffix = "\n\nReview basis:\n" + basis
    report = bench.workspace.create_report_from_sections(matter.matter_id, ACTOR, "Copied basis", "",
        origin_id="report-compilation-" + "e" * 32,
        sections=[{"heading": "Note", "body": "Opening" + suffix, "compilation_basis": basis}])
    section = bench.workspace.report_sections(matter.matter_id, report.report_id)[0]
    copied = "The reviewer intentionally copied this:" + suffix
    response = client.post(f"/matters/{matter.slug}/reports/{report.report_id}/sections/{section.section_id}",
        data={"heading": section.heading, "body": copied, "expected_updated_at": section.updated_at,
              "expected_status": report.status}, follow_redirects=False)
    assert response.status_code == 303
    updated = bench.workspace.report_sections(matter.matter_id, report.report_id)[0]
    assert updated.prose == copied and updated.body == copied + suffix


def test_compilation_completion_audit_retains_report_link_after_deletion(workspace):
    client, bench, matter = workspace
    bench.workspace.create_notebook_item(matter.matter_id, ACTOR, item_type="event", status="confirmed",
        title="Synthetic observed event", body="Synthetic attributed review text.")
    job_id = queue(client, matter, ["notes:active"])
    assert finished(client, matter, job_id)["state"] == "succeeded"
    job = bench.report_compilation.jobs.get(matter.matter_id, ACTOR, job_id)
    report = bench.workspace.report(matter.matter_id, job.report_id)
    response = client.post(f"/matters/{matter.slug}/reports/{report.report_id}/delete",
        data={"expected_updated_at": report.updated_at}, follow_redirects=False)
    assert response.status_code == 303
    event, = [e for e in bench.workspace.audit_events(matter.matter_id) if e.action == "report.compile.complete"]
    assert event.actor_principal_id == ACTOR and event.request_id == job_id
    assert event.object_id == report.report_id and event.details == {"state": "succeeded"}
    assert bench.report_compilation.jobs.get(matter.matter_id, ACTOR, job_id).report_id is None


def test_cancel_and_retry_have_content_free_actor_audit(workspace):
    client, bench, matter = workspace
    bench.report_compilation.coordinator.close()
    job_id = queue(client, matter, ["notes:active"])
    for action in ("cancel", "retry"):
        response = client.post(f"/matters/{matter.slug}/reports/compile/{job_id}/{action}", follow_redirects=False)
        assert response.status_code == 303
    events = [e for e in bench.workspace.audit_events(matter.matter_id) if e.action in {"report.cancel", "report.retry"}]
    assert [(e.action, e.details) for e in events] == [("report.cancel", {"state": "cancelled"}), ("report.retry", {"state": "queued"})]
    assert all(e.actor_principal_id == ACTOR and e.object_id == job_id for e in events)
    bench.report_compilation.jobs.cancel(matter.matter_id, ACTOR, job_id)


@pytest.mark.parametrize("state", ["succeeded", "failed", "cancelled"])
def test_matter_purge_removes_terminal_compilation_jobs(workspace, state):
    client, bench, matter = workspace
    bench.report_compilation.coordinator.close()
    job_id = queue(client, matter, ["notes:active"])
    report = bench.workspace.create_report(matter.matter_id, ACTOR, "Synthetic purge report", "")
    with fixture_write(bench.workspace):
        bench.workspace.connection.execute("UPDATE workbench_report_compilation_job SET state=?,report_id=? WHERE job_id=?",
            (state, report.report_id if state == "succeeded" else None, job_id))
    _, lifecycle = claim_bench_exit(bench, matter, ACTOR)
    completed = bench.workspace.complete_matter_purge(matter.matter_id, lifecycle.purge_id)
    assert completed.state == "deleted"
    assert not bench.workspace.connection.execute("SELECT 1 FROM workbench_report_compilation_job WHERE matter_id=?", (matter.matter_id,)).fetchone()
    assert not bench.workspace.connection.execute("SELECT 1 FROM workbench_report WHERE matter_id=?", (matter.matter_id,)).fetchone()


def test_read_only_administrator_is_not_offered_report_creation(tmp_path):
    from tests.test_matter_management import ADMIN
    app = _app(tmp_path)
    with TestClient(app, base_url="https://testserver") as client:
        csrf = _csrf(client.get('/matters/new', headers=_headers(OWNER)).text)
        slug = _create_matter(client, principal=OWNER, csrf_token=csrf, name="Synthetic read-only reports")
        page = client.get(f"/matters/{slug}/reports?edit=true", headers=_headers(ADMIN))
        assert page.status_code == 200
        assert "Compile saved work" not in page.text and "Make your first report" not in page.text
        assert f'href="/matters/{slug}/reports/new"' not in page.text
        assert client.get(f"/matters/{slug}/reports/new", headers=_headers(ADMIN)).status_code == 403


def test_editor_reserves_compilation_basis_capacity_and_preserves_rejected_draft(workspace):
    client, bench, matter = workspace
    basis = "Synthetic immutable basis."
    report = bench.workspace.create_report_from_sections(matter.matter_id, ACTOR, "Capacity", "",
        origin_id="report-compilation-" + "a" * 32,
        sections=[{"heading": "Note", "body": "Opening\n\nReview basis:\n" + basis, "compilation_basis": basis}])
    section = bench.workspace.report_sections(matter.matter_id, report.report_id)[0]
    assert section.prose_limit == 50_000 - len("\n\nReview basis:\n" + basis)
    edit = client.get(f"/matters/{matter.slug}/reports?report={report.report_id}&edit=true")
    assert f'maxlength="{section.prose_limit}"' in edit.text
    path = f"/matters/{matter.slug}/reports/{report.report_id}/sections/{section.section_id}"
    data = {"heading": section.heading, "body": "x" * section.prose_limit,
            "expected_updated_at": section.updated_at, "expected_status": report.status}
    assert client.post(path, data=data, follow_redirects=False).status_code == 303
    saved = bench.workspace.report_sections(matter.matter_id, report.report_id)[0]
    assert len(saved.body) == 50_000 and saved.compilation_basis == basis
    data.update(body="y" * (section.prose_limit + 1), expected_updated_at=saved.updated_at)
    rejected = client.post(path, data=data, follow_redirects=False)
    assert rejected.status_code == 400 and data["body"] in rejected.text
    assert f'maxlength="{section.prose_limit}"' in rejected.text
    assert bench.workspace.report_sections(matter.matter_id, report.report_id)[0].body == saved.body


def test_compilation_health_and_close_page_track_active_jobs(workspace):
    import re
    client, bench, matter = workspace
    bench.report_compilation.coordinator.close()
    job_id = queue(client, matter, ["notes:active"])
    counts = client.get("/health").json()["report_compilation_jobs"]
    assert counts["queued"] == 1 and counts["running"] == 0
    page = client.get(f"/matters/{matter.slug}/close")
    assert "1 report compilation" in page.text
    assert re.search(r'<button class="button button-primary"[^>]+type="submit"[^>]+disabled', page.text)
    client.post(f"/matters/{matter.slug}/close/begin")
    assert bench.workspace.matter_lifecycle(matter.matter_id).state == "active"
    bench.report_compilation.jobs.cancel(matter.matter_id, ACTOR, job_id)
    counts = client.get("/health").json()["report_compilation_jobs"]
    assert counts["queued"] == 0 and counts["cancelled"] == 1
    page = client.get(f"/matters/{matter.slug}/close")
    assert not re.search(r'<button class="button button-primary"[^>]+type="submit"[^>]+disabled', page.text)
