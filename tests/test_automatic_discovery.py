"""Synthetic regression: suggestions appear after processing, without a model or a person."""
from __future__ import annotations

import re
import shutil
import sqlite3
import threading
import time

import pytest

from tests.case_lifecycle_web_helpers import claim_bench_exit
from fastapi.testclient import TestClient

from case_intelligence.workbench import AutomaticDiscoveryCoordinator, create_workbench_app
from case_intelligence.workspace_store import AUTOMATIC_DISCOVERY_PRINCIPAL, WorkspaceStore
from tests.test_matter_notebook import WEB_ACTOR, _create_matter

FIRST = b"Alex Example met Jordan Sample. Amber Cooperative opened on 03/04/2026."
SECOND = b"Alex Example met Riley Placeholder. ID: ZX-204."


def upload(client, slug, name, content):
    response = client.post(f"/matters/{slug}/uploads", files=[("files", (name, content, "text/plain"))])
    assert response.status_code in (200, 303), response.text


@pytest.fixture
def workbench(tmp_path, monkeypatch):
    monkeypatch.setenv("CASE_INTELLIGENCE_STORAGE_RESERVE_GIB", "0")
    monkeypatch.delenv("CASE_INTELLIGENCE_AUTOMATIC_DISCOVERY", raising=False)
    runtime = tmp_path / "runtime"
    with TestClient(create_workbench_app(runtime, auth_mode="test")) as client:
        slug = _create_matter(client)
        bench = client.app.state.workbench
        yield client, bench, bench.matter(slug, WEB_ACTOR), runtime


def names(bench, matter):
    service = bench.entity_service(matter)
    rows, _total = service.list(matter.matter_id, WEB_ACTOR)
    return sorted((row["display_name"], row["status"]) for row in rows)


def test_processing_yields_suggestions_without_a_model_or_a_review_run(workbench):
    client, bench, matter, _runtime = workbench
    assert bench.automatic_discovery is None  # Off unless the deployment enables it.
    upload(client, matter.slug, "Synthetic memo.txt", FIRST)
    assert bench.run_automatic_discovery_once() == 1
    assert names(bench, matter) == [
        ("03/04/2026", "suggested"), ("Alex Example", "suggested"),
        ("Amber Cooperative", "suggested"), ("Jordan Sample", "suggested")]
    assert bench.run_automatic_discovery_once() == 0
    assert bench.workspace.connection.execute(
        "SELECT COUNT(*) FROM workbench_review_run WHERE matter_id=?", (matter.matter_id,)).fetchone()[0] == 0
    progress = bench.automatic_entity_discovery(matter).automatic_progress(matter.matter_id, detail=True)
    assert progress == dict(pending=0, processed=1, failed=0, unsealed_sources=0, sources_attention=0,
                            sources_complete=1, sources_ready=1, sources_pending=0, suggested=4,
                            budget_reached=False)
    # Suggestions are attributed to the inactive system principal.
    creators = {row[0] for row in bench.workspace.connection.execute(
        "SELECT created_by FROM workbench_entity WHERE matter_id=?", (matter.matter_id,))}
    assert creators == {AUTOMATIC_DISCOVERY_PRINCIPAL}
    page = client.get(f"/matters/{matter.slug}/entities")
    assert page.status_code == 200 and "Jordan Sample" in page.text


def test_new_version_supersedes_without_touching_human_decisions(workbench):
    client, bench, matter, _runtime = workbench
    upload(client, matter.slug, "Synthetic memo.txt", FIRST)
    bench.run_automatic_discovery_once()
    service = bench.entity_service(matter)
    rows, _ = service.list(matter.matter_id, WEB_ACTOR)
    jordan = next(row for row in rows if row["display_name"] == "Jordan Sample")
    service.update(matter.matter_id, WEB_ACTOR, jordan["entity_id"], expected_revision=jordan["revision"],
                   display_name="Jordan Sample (reviewed)", status="confirmed")
    store = bench.source_store(matter)
    document = next(iter(store.documents.values()))
    old_version = document.version_id
    # A second source is discovered; the reviewed identity is untouched.
    upload(client, matter.slug, "Synthetic second memo.txt", SECOND)
    assert bench.run_automatic_discovery_once() == 1
    rows, _ = service.list(matter.matter_id, WEB_ACTOR)
    assert ("Jordan Sample (reviewed)", "confirmed") in {(row["display_name"], row["status"]) for row in rows}
    assert "Riley Placeholder" in {row["display_name"] for row in rows}
    # A newer version of a source seals afresh and stops unfinished older work.
    repo = bench.workspace.entity_repository(automatic=True)
    current = dict(document_id=document.document_id, source_version_id="synthetic-newer-version",
                   content_basis_digest="")
    with repo.transaction(matter.matter_id, AUTOMATIC_DISCOVERY_PRINCIPAL):
        repo.connection.execute(
            "INSERT INTO workbench_entity_auto_discovery_unit (matter_id,document_id,source_version_id,"
            "content_basis_digest,unit_ordinal,unit_digest,extractor_version,state) VALUES (?,?,?,?,?,?,?,?)",
            (matter.matter_id, document.document_id, old_version, "", 9, "0" * 64, "synthetic-older", "pending"))
        assert repo.seal_auto_discovery(matter.matter_id, current, "synthetic-older", [(1, "1" * 64)])
        assert not repo.seal_auto_discovery(matter.matter_id, current, "synthetic-older", [(1, "1" * 64)])
        remaining = repo.connection.execute(
            "SELECT COUNT(*) FROM workbench_entity_auto_discovery_unit WHERE matter_id=? AND source_version_id=?",
            (matter.matter_id, old_version)).fetchone()[0]
    # Superseded ledger rows are dropped; suggestions and decisions are untouched.
    assert remaining == 0
    assert ("Jordan Sample (reviewed)", "confirmed") in names(bench, matter)


def test_only_the_system_principal_on_an_active_matter_is_authorized(workbench):
    _client, bench, matter, _runtime = workbench
    repo = bench.workspace.entity_repository(automatic=True)
    with pytest.raises(KeyError):
        with repo.transaction(matter.matter_id, WEB_ACTOR):
            pass
    with pytest.raises(KeyError):
        with repo.transaction("ci-matter-" + "0" * 32, AUTOMATIC_DISCOVERY_PRINCIPAL):
            pass
    # The system principal cannot use the ordinary membership path.
    with pytest.raises(KeyError):
        with bench.workspace.entity_repository().transaction(matter.matter_id, AUTOMATIC_DISCOVERY_PRINCIPAL):
            pass
    lookup = "SELECT active FROM workbench_principal WHERE principal_id=?"
    # Refused attempts create nothing; the principal appears on first real use.
    assert bench.workspace.connection.execute(lookup, (AUTOMATIC_DISCOVERY_PRINCIPAL,)).fetchone() is None
    with repo.transaction(matter.matter_id, AUTOMATIC_DISCOVERY_PRINCIPAL):
        pass
    assert bench.workspace.connection.execute(lookup, (AUTOMATIC_DISCOVERY_PRINCIPAL,)).fetchone()[0] == 0
    assert AUTOMATIC_DISCOVERY_PRINCIPAL not in {
        principal.principal_id for principal in bench.workspace.active_principals()}


def test_a_fresh_store_still_starts_without_principals(tmp_path):
    store = WorkspaceStore(tmp_path / "workbench.sqlite")
    try:
        assert store.connection.execute("SELECT COUNT(*) FROM workbench_principal").fetchone()[0] == 0
    finally:
        store.close()


def test_coordinator_runs_after_a_source_becomes_ready(tmp_path, monkeypatch):
    monkeypatch.setenv("CASE_INTELLIGENCE_STORAGE_RESERVE_GIB", "0")
    monkeypatch.setenv("CASE_INTELLIGENCE_AUTOMATIC_DISCOVERY", "1")
    with TestClient(create_workbench_app(tmp_path / "runtime", auth_mode="test")) as client:
        bench = client.app.state.workbench
        assert isinstance(bench.automatic_discovery, AutomaticDiscoveryCoordinator)
        slug = _create_matter(client)
        matter = bench.matter(slug, WEB_ACTOR)
        upload(client, slug, "Synthetic memo.txt", FIRST)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and len(names(bench, matter)) < 4:
            time.sleep(0.05)
        assert ("Jordan Sample", "suggested") in names(bench, matter)
        assert bench.automatic_discovery.status() == {"enabled": True, "last_error": ""}


def test_restore_export_purge_and_idempotent_migration(workbench, tmp_path):
    client, bench, matter, runtime = workbench
    upload(client, matter.slug, "Synthetic memo.txt", FIRST)
    bench.run_automatic_discovery_once()
    expected = bench.workspace.entity_repository().discovery_export(matter.matter_id)
    # One compact row per source inventory, not one per unit.
    [coverage] = expected["automatic_coverage"]
    assert (coverage["inventory_state"], coverage["units"], coverage["processed"], coverage["pending"]) == (
        "processed", 1, 1, 0)
    assert expected["occurrence_tombstones"]
    before = names(bench, matter)
    client.__exit__(None, None, None)
    restored = tmp_path / "restored"
    shutil.copytree(runtime, restored)
    with TestClient(create_workbench_app(restored, auth_mode="test")) as again:
        bench = again.app.state.workbench
        matter = bench.matter(matter.slug, WEB_ACTOR)
        assert bench.workspace.entity_repository().discovery_export(matter.matter_id) == expected
        assert names(bench, matter) == before
        # Already discovered; a restored node does not duplicate suggestions.
        assert bench.run_automatic_discovery_once() == 0
        _, lifecycle = claim_bench_exit(bench, matter, WEB_ACTOR)
        bench.workspace.complete_matter_purge(matter.matter_id, lifecycle.purge_id)
        count = bench.workspace.connection.execute(
            "SELECT COUNT(*) FROM workbench_entity_auto_discovery_unit WHERE matter_id=?",
            (matter.matter_id,)).fetchone()[0]
        assert count == 0
    # Re-running every migration on the restored store changes nothing.
    database = restored / "workbench.sqlite"
    before_schema = sqlite3.connect(database).execute("SELECT sql FROM sqlite_master ORDER BY name").fetchall()
    WorkspaceStore(database).connection.close()
    after_schema = sqlite3.connect(database).execute("SELECT sql FROM sqlite_master ORDER BY name").fetchall()
    assert before_schema == after_schema


def test_home_readiness_reports_discovery_and_links_to_suggestions(workbench, monkeypatch):
    client, bench, matter, _runtime = workbench
    upload(client, matter.slug, "Synthetic memo.txt", FIRST)
    status = client.get(f"/matters/{matter.slug}/processing-status").json()
    assert status["discovery"] == {"label": "", "href": "", "working": False}  # Disabled deployment.
    class Enabled:
        def close(self):
            pass
    monkeypatch.setattr(bench, "automatic_discovery", Enabled())
    status = client.get(f"/matters/{matter.slug}/processing-status").json()
    assert status["discovery"]["label"] == "Finding people and dates · 0 of 1 sources"
    assert status["discovery"]["working"] is True
    bench.run_automatic_discovery_once()
    status = client.get(f"/matters/{matter.slug}/processing-status").json()
    assert status["discovery"] == {
        "label": "4 suggested people, things and dates to review",
        "href": f"/matters/{matter.slug}/entities", "working": False}
    home = client.get(f"/matters/{matter.slug}/home").text
    assert f'data-readiness-discovery href="/matters/{matter.slug}/entities">4 suggested' in home


def test_system_provider_is_reserved_for_internal_principals(workbench):
    from case_intelligence.workspace_store import WorkspaceProblem
    _client, bench, matter, _runtime = workbench
    repo = bench.workspace.entity_repository(automatic=True)
    with repo.transaction(matter.matter_id, AUTOMATIC_DISCOVERY_PRINCIPAL):
        pass
    with pytest.raises(WorkspaceProblem):
        bench.workspace.upsert_principal("system", "automatic-discovery", "Impostor", "impostor")
    row = bench.workspace.connection.execute(
        "SELECT display_name, active FROM workbench_principal WHERE principal_id=?",
        (AUTOMATIC_DISCOVERY_PRINCIPAL,)).fetchone()
    assert tuple(row) == ("Automatic discovery", 0)


def long_text(units, marker):
    # Text sources split into 20-line units; one synthetic name per unit.
    return "\n".join(f"{marker} Example{index // 20} called on line {index}." for index in range(units * 20)).encode()


def test_sealed_sources_load_only_pending_units(workbench, monkeypatch):
    client, bench, matter, _runtime = workbench
    upload(client, matter.slug, "Synthetic long log.txt", long_text(30, "Alex"))
    calls = []
    original = bench.automatic_entity_discovery
    def counting(target, should_stop=None):
        discovery = original(target, should_stop)
        loader = discovery.load_document
        def load_document(document_id, source_version_id, ordinals=None):
            calls.append(None if ordinals is None else len(ordinals))
            return loader(document_id, source_version_id, ordinals)
        discovery.load_document = load_document
        return discovery
    monkeypatch.setattr(bench, "automatic_entity_discovery", counting)
    assert bench.run_automatic_discovery_once(unit_limit=25) == 25
    assert bench.run_automatic_discovery_once() == 5
    # One full inventory pass, then only the pending ordinals.
    assert calls == [None, 25, 5]
    assert bench.run_automatic_discovery_once() == 0 and calls == [None, 25, 5]


def test_unavailable_sources_do_not_stall_a_run(workbench, monkeypatch):
    client, bench, matter, _runtime = workbench
    for index in range(7):
        upload(client, matter.slug, f"Synthetic memo {index}.txt", f"Riley Placeholder{index} arrived.".encode())
    documents = sorted(bench.source_store(matter).documents)
    readable = documents[-1]
    original = bench.automatic_entity_discovery
    def mostly_unavailable(target, should_stop=None):
        discovery = original(target, should_stop)
        loader = discovery.load_document
        def load_document(document_id, source_version_id, ordinals=None):
            if document_id != readable:
                raise KeyError(document_id)
            return loader(document_id, source_version_id, ordinals)
        discovery.load_document = load_document
        return discovery
    monkeypatch.setattr(bench, "automatic_entity_discovery", mostly_unavailable)
    # Six unavailable sources seal as failed first; the run continues to the seventh.
    assert bench.run_automatic_discovery_once() == 1
    progress = bench.automatic_entity_discovery(matter).automatic_progress(matter.matter_id, detail=True)
    assert progress["unsealed_sources"] == 0 and progress["processed"] == 1


def test_changed_extracted_text_is_rediscovered(workbench):
    client, bench, matter, _runtime = workbench
    upload(client, matter.slug, "Synthetic memo.txt", FIRST)
    assert bench.run_automatic_discovery_once() == 1
    document = next(iter(bench.source_store(matter).documents.values()))
    # Same source version, new extracted basis (for example an OCR retry).
    with bench.workspace._lock, bench.workspace.connection:
        bench.workspace.connection.execute(
            "UPDATE workbench_source_catalog SET content_basis_digest=? WHERE matter_id=? AND document_id=?",
            ("9" * 64, matter.matter_id, document.document_id))
    discovery = bench.automatic_entity_discovery(matter)
    assert discovery.automatic_progress(matter.matter_id)["unsealed_sources"] == 1
    assert bench.run_automatic_discovery_once() == 1
    seals = bench.workspace.connection.execute(
        "SELECT content_basis_digest FROM workbench_entity_auto_discovery_unit WHERE matter_id=? "
        "AND unit_ordinal=0 ORDER BY content_basis_digest", (matter.matter_id,)).fetchall()
    assert [row[0] for row in seals] == ["9" * 64]  # The superseded basis's ledger rows are dropped.
    # The same passages are not suggested twice.
    assert len(names(bench, matter)) == 4


def test_every_automatic_unit_is_audited(workbench):
    client, bench, matter, _runtime = workbench
    upload(client, matter.slug, "Synthetic memo.txt", FIRST)
    upload(client, matter.slug, "Synthetic second memo.txt", SECOND)
    assert bench.run_automatic_discovery_once() == 2
    events = [event for event in bench.workspace.audit_events(matter.matter_id)
              if event.action == "entity.discovery_unit"]
    assert len(events) == 2
    assert {event.actor_principal_id for event in events} == {AUTOMATIC_DISCOVERY_PRINCIPAL}
    assert {event.outcome for event in events} == {"success"}
    assert {event.object_type for event in events} == {"source"}


def test_a_failing_audit_rolls_back_its_unit(workbench, monkeypatch):
    client, bench, matter, _runtime = workbench
    upload(client, matter.slug, "Synthetic memo.txt", FIRST)
    original = bench.workspace._append_audit_event_locked
    def refuse(**_values):
        raise RuntimeError("synthetic audit outage")
    monkeypatch.setattr(bench.workspace, "_append_audit_event_locked", refuse)
    result = bench.automatic_discovery_pass()
    assert result.units == 0 and result.failed_matters == (matter.matter_id,)
    # Nothing from the unit committed without its audit record.
    assert names(bench, matter) == []
    states = {row[0] for row in bench.workspace.connection.execute(
        "SELECT state FROM workbench_entity_auto_discovery_unit WHERE matter_id=? AND unit_ordinal>0",
        (matter.matter_id,))}
    assert states == {"pending"}
    monkeypatch.setattr(bench.workspace, "_append_audit_event_locked", original)
    assert bench.run_automatic_discovery_once() == 1
    assert len(names(bench, matter)) == 4
    assert len([event for event in bench.workspace.audit_events(matter.matter_id)
                if event.action == "entity.discovery_unit"]) == 1


def test_one_failing_matter_does_not_stop_the_others(workbench, monkeypatch):
    client, bench, matter, _runtime = workbench
    other = bench.matter(_create_matter(client, "Synthetic second matter"), WEB_ACTOR)
    upload(client, matter.slug, "Synthetic memo.txt", FIRST)
    upload(client, other.slug, "Synthetic memo.txt", SECOND)
    original = bench.automatic_entity_discovery
    def broken_first(target, should_stop=None):
        if target.matter_id == min(matter.matter_id, other.matter_id):
            raise RuntimeError("synthetic unreadable source registry")
        return original(target, should_stop)
    monkeypatch.setattr(bench, "automatic_entity_discovery", broken_first)
    result = bench.automatic_discovery_pass()
    assert result.failed_matters == (min(matter.matter_id, other.matter_id),)
    assert result.units == 1


def test_sealing_is_reported_as_progress_when_the_step_cap_is_reached(workbench, monkeypatch):
    import case_intelligence.workbench as workbench_module
    client, bench, matter, _runtime = workbench
    for index in range(7):
        upload(client, matter.slug, f"Synthetic memo {index}.txt", f"Riley Placeholder{index} arrived.".encode())
    original = bench.automatic_entity_discovery
    def unavailable(target, should_stop=None):
        discovery = original(target, should_stop)
        def load_document(document_id, source_version_id, ordinals=None):
            raise KeyError(document_id)
        discovery.load_document = load_document
        return discovery
    monkeypatch.setattr(bench, "automatic_entity_discovery", unavailable)
    monkeypatch.setattr(workbench_module, "MAX_AUTOMATIC_DISCOVERY_STEPS", 1)
    result = bench.automatic_discovery_pass()
    # One capped step sealed five sources: no units, but real progress to continue from.
    assert result.units == 0 and result.progress == 5 and not result.failed_matters
    assert bench.automatic_discovery_pass().progress == 2
    assert bench.automatic_discovery_pass().progress == 0


def test_incomplete_coverage_needs_attention_and_can_be_retried(workbench, monkeypatch):
    client, bench, matter, _runtime = workbench
    woken = []
    class Enabled:
        def wake(self, matter_id):
            woken.append(matter_id)
        def close(self):
            pass
    monkeypatch.setattr(bench, "automatic_discovery", Enabled())
    upload(client, matter.slug, "Synthetic memo.txt", FIRST)
    original = bench.automatic_entity_discovery
    def pending_units_unavailable(target, should_stop=None):
        discovery = original(target, should_stop)
        loader = discovery.load_document
        def load_document(document_id, source_version_id, ordinals=None):
            if ordinals is None:
                return loader(document_id, source_version_id)
            return iter(())  # The inventory succeeds, then the unit's text is unavailable.
        discovery.load_document = load_document
        return discovery
    monkeypatch.setattr(bench, "automatic_entity_discovery", pending_units_unavailable)
    assert bench.run_automatic_discovery_once() == 1
    progress = bench.automatic_discovery_progress(matter)
    assert progress["sources_complete"] == 0 and progress["sources_attention"] == 1
    status = client.get(f"/matters/{matter.slug}/processing-status").json()
    assert status["discovery"] == {
        "label": "Finding people and dates needs attention · 1 source",
        "href": f"/matters/{matter.slug}/entities", "working": False}
    page = client.get(f"/matters/{matter.slug}/entities").text
    assert "Automatic discovery needs attention" in page
    assert 'name="action" value="retry_automatic"' in page
    assert names(bench, matter) == []
    monkeypatch.setattr(bench, "automatic_entity_discovery", original)
    woken.clear()
    response = client.post(f"/matters/{matter.slug}/entities/actions",
                           data=dict(action="retry_automatic"), follow_redirects=False)
    assert response.status_code == 303
    assert woken == [matter.matter_id]
    assert ledger_states(bench, matter) == ["pending"]
    assert bench.run_automatic_discovery_once() == 1
    progress = bench.automatic_discovery_progress(matter)
    assert progress["sources_complete"] == 1 and progress["sources_attention"] == 0
    assert len(names(bench, matter)) == 4
    assert "Automatic discovery needs attention" not in client.get(f"/matters/{matter.slug}/entities").text
    assert "entity.retry_automatic" in [event.action for event in bench.workspace.audit_events(matter.matter_id)]


def test_attention_is_shown_while_other_units_are_still_pending(workbench, monkeypatch):
    client, bench, matter, _runtime = workbench
    class Enabled:
        def wake(self, matter_id):
            pass
        def close(self):
            pass
    monkeypatch.setattr(bench, "automatic_discovery", Enabled())
    upload(client, matter.slug, "Synthetic long log.txt", long_text(30, "Alex"))
    assert bench.run_automatic_discovery_once(unit_limit=25) == 25
    # One processed unit later failed; five units are still pending.
    with bench.workspace._lock, bench.workspace.connection as connection:  # Shared with background readers.
        connection.execute(
            "UPDATE workbench_entity_auto_discovery_unit SET state='failed' WHERE matter_id=? AND unit_ordinal=1",
            (matter.matter_id,))
    progress = bench.automatic_discovery_progress(matter)
    assert progress["sources_pending"] == 1 and progress["sources_attention"] == 1
    status = client.get(f"/matters/{matter.slug}/processing-status").json()
    assert status["discovery"]["label"] == "Finding people and dates needs attention · 1 source"
    # With other sources still being searched, progress continues to show the attention count.
    upload(client, matter.slug, "Synthetic memo.txt", FIRST)
    status = client.get(f"/matters/{matter.slug}/processing-status").json()
    assert status["discovery"]["label"] == "Finding people and dates · 0 of 2 sources · 1 needs attention"
    assert 'name="action" value="retry_automatic"' in client.get(f"/matters/{matter.slug}/entities").text


def test_selected_units_are_processed_as_they_are_read(workbench, monkeypatch):
    client, bench, matter, _runtime = workbench
    upload(client, matter.slug, "Synthetic long log.txt", long_text(30, "Alex"))
    events = []
    original = bench.automatic_entity_discovery
    def recording(target, should_stop=None):
        discovery = original(target, should_stop)
        loader = discovery.load_document
        def load_document(document_id, source_version_id, ordinals=None):
            for ordinal, text, reference in loader(document_id, source_version_id, ordinals):
                if ordinals is not None:
                    events.append(("read", ordinal))
                yield ordinal, text, reference
        discovery.load_document = load_document
        return discovery
    monkeypatch.setattr(bench, "automatic_entity_discovery", recording)
    discovery = bench.automatic_entity_discovery(matter)
    handled, _sealed, _retried = discovery.automatic_step(
        matter.matter_id, unit_limit=3, audit_unit=lambda event: events.append(("processed", event["unit_ordinal"])))
    assert handled == 3
    # Never more than one unit's text is held before it is processed.
    assert events == [("read", 1), ("processed", 1), ("read", 2), ("processed", 2), ("read", 3), ("processed", 3)]


def test_a_full_discovery_budget_is_reported_as_paused_and_can_resume(workbench, monkeypatch):
    from case_intelligence.entity_discovery import DISCOVERY_BYTE_LIMIT
    from case_intelligence.entity_repository import EntityRepository
    client, bench, matter, _runtime = workbench
    woken = []
    class Enabled:
        def wake(self, matter_id):
            woken.append(matter_id)
        def close(self):
            pass
    monkeypatch.setattr(bench, "automatic_discovery", Enabled())
    upload(client, matter.slug, "Synthetic memo.txt", FIRST)
    upload(client, matter.slug, "Synthetic call.txt", SECOND)
    assert bench.run_automatic_discovery_once(unit_limit=1) == 1  # One source's suggestions are saved.
    upload(client, matter.slug, "Synthetic note.txt", b"nothing of note was recorded here.")
    measured = EntityRepository.discovery_storage_bytes
    headroom = {"bytes": 5000}
    monkeypatch.setattr(EntityRepository, "discovery_storage_bytes",
                        lambda self, matter_id: DISCOVERY_BYTE_LIMIT - headroom["bytes"])
    # 5,000 bytes pass the fixed pre-check but not a unit with a new occurrence. That
    # unit waits, while the later unit without occurrences still completes.
    assert bench.run_automatic_discovery_once() == 1
    progress = bench.automatic_discovery_progress(matter)
    assert progress["sources_pending"] == 1 and progress["budget_reached"] is True
    status = client.get(f"/matters/{matter.slug}/processing-status").json()
    assert status["discovery"] == {
        "label": "Finding people and dates paused · suggestion storage limit reached",
        "href": f"/matters/{matter.slug}/entities", "working": False}
    page = client.get(f"/matters/{matter.slug}/entities").text
    assert "Automatic discovery is paused" in page
    assert "delete suggestions you do not need" in page
    assert "those records alone can fill it" in page  # The terminal case is stated honestly.
    assert 'name="action" value="retry_automatic"' in page
    # With no headroom at all, newly queued work is marked as waiting at once.
    upload(client, matter.slug, "Synthetic later.txt", FIRST + b" Riley Placeholder followed up.")
    headroom["bytes"] = 0
    assert bench.run_automatic_discovery_once() == 0
    progress = bench.automatic_discovery_progress(matter)
    assert progress["sources_pending"] == 2 and progress["budget_reached"] is True
    monkeypatch.setattr(EntityRepository, "discovery_storage_bytes", measured)
    # Deleting a suggestion really frees counted space (its mentions and history go with it).
    service = bench.entity_service(matter)
    rows, _ = service.list(matter.matter_id, WEB_ACTOR)
    with service.repository.transaction(matter.matter_id, WEB_ACTOR) as repo:
        before = repo.discovery_storage_bytes(matter.matter_id)
    service.delete(matter.matter_id, WEB_ACTOR, rows[0]["entity_id"], expected_revision=rows[0]["revision"])
    with service.repository.transaction(matter.matter_id, WEB_ACTOR) as repo:
        assert repo.discovery_storage_bytes(matter.matter_id) < before - 1000
    # Check again wakes the worker; the waiting units then complete and the pause clears.
    woken.clear()
    response = client.post(f"/matters/{matter.slug}/entities/actions",
                           data=dict(action="retry_automatic"), follow_redirects=False)
    assert response.status_code == 303 and woken == [matter.matter_id]
    assert bench.run_automatic_discovery_once() == 2
    progress = bench.automatic_discovery_progress(matter)
    assert progress["sources_pending"] == 0 and progress["budget_reached"] is False
    assert "Automatic discovery is paused" not in client.get(f"/matters/{matter.slug}/entities").text


def test_units_waiting_for_space_do_not_hold_up_smaller_units(workbench, monkeypatch):
    from case_intelligence.entity_discovery import DiscoveryUnitTooLarge, EntityDiscovery
    client, bench, matter, _runtime = workbench
    upload(client, matter.slug, "Synthetic long log.txt", long_text(60, "Alex"))
    original = EntityDiscovery._process_unit
    def first_50_too_large(self, repo, matter_id, actor_id, unit, loaded, ledger):
        if unit["unit_ordinal"] <= 50:
            raise DiscoveryUnitTooLarge("synthetic reservation does not fit")
        return original(self, repo, matter_id, actor_id, unit, loaded, ledger)
    monkeypatch.setattr(EntityDiscovery, "_process_unit", first_50_too_large)
    # Two whole 25-unit steps only mark units as waiting; marking them counts as
    # progress, so the same pass still reaches the ten units that fit.
    assert bench.run_automatic_discovery_once() == 10
    waiting = bench.workspace.connection.execute(
        "SELECT COUNT(*) FROM workbench_entity_auto_discovery_unit WHERE matter_id=? AND state='pending' "
        "AND note<>''", (matter.matter_id,)).fetchone()[0]
    assert waiting == 50
    # Repeated attempts at already-marked units are not progress, so the
    # worker waits for the next sweep instead of retrying at once.
    assert bench.automatic_discovery_pass().progress == 0


def test_partly_freed_space_reaches_later_units_without_a_retry(workbench, monkeypatch):
    from case_intelligence.entity_discovery import DISCOVERY_BYTE_LIMIT, DiscoveryUnitTooLarge, EntityDiscovery
    from case_intelligence.entity_repository import EntityRepository
    client, bench, matter, _runtime = workbench
    upload(client, matter.slug, "Synthetic long log.txt", long_text(60, "Alex"))
    measured = EntityRepository.discovery_storage_bytes
    monkeypatch.setattr(EntityRepository, "discovery_storage_bytes",
                        lambda self, matter_id: DISCOVERY_BYTE_LIMIT)
    assert bench.run_automatic_discovery_once() == 0
    assert bench.automatic_entity_discovery(matter).automatic_progress(matter.matter_id)["budget_reached"]
    # Some space is freed: the first 25 queued units still do not fit, later ones do.
    monkeypatch.setattr(EntityRepository, "discovery_storage_bytes", measured)
    original = EntityDiscovery._process_unit
    def first_25_too_large(self, repo, matter_id, actor_id, unit, loaded, ledger):
        if unit["unit_ordinal"] <= 25:
            raise DiscoveryUnitTooLarge("synthetic reservation does not fit")
        return original(self, repo, matter_id, actor_id, unit, loaded, ledger)
    monkeypatch.setattr(EntityDiscovery, "_process_unit", first_25_too_large)
    # Units that still do not fit rotate behind the others, so an ordinary pass
    # reaches every unit that fits without anyone pressing Check again.
    assert bench.run_automatic_discovery_once() == 35
    rows = dict(bench.workspace.connection.execute(
        "SELECT state, COUNT(*) FROM workbench_entity_auto_discovery_unit WHERE matter_id=? "
        "AND unit_ordinal>0 GROUP BY state", (matter.matter_id,)).fetchall())
    assert rows == {"pending": 25, "processed": 35}


def test_a_status_poll_never_creates_the_system_principal(workbench):
    client, bench, matter, _runtime = workbench
    connection = bench.workspace.connection
    principal = "SELECT COUNT(*) FROM workbench_principal WHERE principal_id=?"
    with bench.workspace._lock:
        assert connection.execute(principal, (AUTOMATIC_DISCOVERY_PRINCIPAL,)).fetchone()[0] == 0
    statements = []
    with bench.workspace._lock:
        connection.set_trace_callback(statements.append)
    try:
        progress = bench.automatic_entity_discovery(matter).automatic_progress(matter.matter_id)
        status = client.get(f"/matters/{matter.slug}/processing-status")
    finally:
        with bench.workspace._lock:
            connection.set_trace_callback(None)
    assert status.status_code == 200 and progress["sources_complete"] == 0
    # Only writes whose target is the principal table count; background work on the
    # shared connection may join it (for example a report-job membership check).
    principal_write = re.compile(r"^\s*(?:INSERT(?:\s+OR\s+\w+)?\s+INTO|REPLACE\s+INTO|UPDATE(?:\s+OR\s+\w+)?|DELETE\s+FROM)"
                                 r"\s+workbench_principal\b", re.I)
    assert not [sql for sql in statements if principal_write.match(sql)]
    with bench.workspace._lock:
        assert connection.execute(principal, (AUTOMATIC_DISCOVERY_PRINCIPAL,)).fetchone()[0] == 0


def test_readiness_progress_never_scans_the_coverage_ledger(workbench):
    """The readiness poll answers with index seeks per source, not a ledger scan."""
    client, bench, matter, _runtime = workbench
    upload(client, matter.slug, "Synthetic long log.txt", long_text(30, "Alex"))
    bench.run_automatic_discovery_once()
    statements = []
    connection = bench.workspace.connection
    poll_thread = threading.get_ident()
    # The connection is shared: idle report claims also open writer transactions.
    # Attribute every statement in this synchronous poll to its calling thread.
    def trace_poll(sql):
        if threading.get_ident() == poll_thread:
            statements.append(sql)
    with bench.workspace._lock:
        connection.set_trace_callback(trace_poll)
    try:
        bench.automatic_entity_discovery(matter).automatic_progress(matter.matter_id)
    finally:
        with bench.workspace._lock:
            connection.set_trace_callback(None)
    queries = [sql for sql in statements if sql.lstrip().upper().startswith("SELECT")]
    assert queries, "the readiness poll must be traced"
    assert not [sql for sql in statements if "BEGIN IMMEDIATE" in sql.upper()]
    # Authorization included: the poll executes no write statement at all, so the
    # deferred transaction never becomes a writer.
    assert not [sql for sql in statements
                if sql.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE", "REPLACE"))]
    assert "GROUP BY" not in " ".join(queries).upper()
    for sql in queries:
        with bench.workspace._lock:
            plan = " ".join(row[3] for row in connection.execute("EXPLAIN QUERY PLAN " + sql))
        assert "SCAN a" not in plan and "SCAN workbench_entity_auto_discovery_unit" not in plan, (sql, plan)


def test_a_failed_seal_is_inventoried_again_on_retry(workbench, monkeypatch):
    client, bench, matter, _runtime = workbench
    upload(client, matter.slug, "Synthetic memo.txt", FIRST)
    original = bench.automatic_entity_discovery
    def unreadable(target, should_stop=None):
        discovery = original(target, should_stop)
        def load_document(document_id, source_version_id, ordinals=None):
            raise KeyError(document_id)
        discovery.load_document = load_document
        return discovery
    monkeypatch.setattr(bench, "automatic_entity_discovery", unreadable)
    bench.run_automatic_discovery_once()
    assert original(matter).automatic_progress(matter.matter_id)["sources_attention"] == 1
    monkeypatch.setattr(bench, "automatic_entity_discovery", original)
    # A reviewer retry (the same call the page's action makes) re-inventories the source.
    assert bench.entity_discovery(matter).retry_automatic(matter.matter_id, WEB_ACTOR) == 1
    assert bench.run_automatic_discovery_once() == 1
    assert len(names(bench, matter)) == 4


def test_a_colliding_principal_is_never_trusted(workbench):
    from case_intelligence.workspace_store import WorkspaceProblem
    _client, bench, matter, _runtime = workbench
    # Ordinary principal creation cannot claim the internal identifier.
    with pytest.raises(WorkspaceProblem):
        bench.workspace.upsert_principal("local", "impostor", "Impostor", "impostor",
                                         preferred_principal_id=AUTOMATIC_DISCOVERY_PRINCIPAL)
    # A pre-existing row that is not exactly the inactive internal principal is refused.
    with bench.workspace._lock, bench.workspace.connection:
        bench.workspace.connection.execute(
            "INSERT INTO workbench_principal(principal_id,provider,provider_subject,display_name,login_name,"
            "active,created_at,last_seen_at) VALUES (?,?,?,?,?,1,?,?)",
            (AUTOMATIC_DISCOVERY_PRINCIPAL, "local", "impostor", "Impostor", "impostor",
             "1970-01-01T00:00:00Z", "1970-01-01T00:00:00Z"))
    repo = bench.workspace.entity_repository(automatic=True)
    with pytest.raises(KeyError):
        with repo.transaction(matter.matter_id, AUTOMATIC_DISCOVERY_PRINCIPAL):
            pass


def test_first_coordinator_pass_always_sweeps(tmp_path, monkeypatch):
    import case_intelligence.workbench as workbench_module
    swept = []
    monkeypatch.setattr(workbench_module, "monotonic", lambda: 1.0)  # Just after host boot.
    coordinator = AutomaticDiscoveryCoordinator(
        lambda ids, **_: swept.append(ids) or workbench_module.AutomaticDiscoveryPass(0, 0), sweep_seconds=900)
    try:
        deadline = time.time() + 5
        while not swept and time.time() < deadline:
            time.sleep(0.01)
        assert swept[0] is None  # None means "sweep every active matter".
    finally:
        coordinator.close()


def test_a_skipped_matter_is_recorded_once_for_administrators(workbench, monkeypatch):
    client, bench, matter, _runtime = workbench
    upload(client, matter.slug, "Synthetic memo.txt", FIRST)
    def broken(target, should_stop=None):
        raise RuntimeError("synthetic unreadable source registry")
    monkeypatch.setattr(bench, "automatic_entity_discovery", broken)
    bench.automatic_discovery_pass()
    bench.automatic_discovery_pass()
    events = [event for event in bench.workspace.audit_events(matter.matter_id)
              if event.action == "entity.discovery_automatic"]
    assert len(events) == 1 and events[0].outcome == "failure"
    assert "synthetic" not in repr(events[0].details)


def test_ledger_tracks_only_current_sources_and_stays_outside_the_budget(workbench, monkeypatch):
    client, bench, matter, _runtime = workbench
    upload(client, matter.slug, "Synthetic long log.txt", long_text(30, "Alex"))
    used = bench.workspace.entity_repository().discovery_storage_bytes
    before = used(matter.matter_id)
    original = bench.automatic_entity_discovery
    def tight(target, should_stop=None):
        discovery = original(target, should_stop)
        discovery.byte_limit = max(4096, used(target.matter_id) + 4096)
        return discovery
    monkeypatch.setattr(bench, "automatic_entity_discovery", tight)
    # The inventory is always recorded; suggestions stop at the budget. The seal and
    # the 30 units newly marked as waiting for space count as scheduling progress.
    result = bench.automatic_discovery_pass()
    assert result.units == 0 and result.progress == 31
    count = "SELECT COUNT(*) FROM workbench_entity_auto_discovery_unit WHERE matter_id=?"
    assert bench.workspace.connection.execute(count, (matter.matter_id,)).fetchone()[0] == 31
    assert used(matter.matter_id) == before
    # A removed source's ledger rows are pruned on the next pass.
    document = next(iter(bench.source_store(matter).documents.values()))
    bench.workspace.delete_source_catalog(matter.matter_id, (document.document_id,))
    bench.automatic_discovery_pass()
    assert bench.workspace.connection.execute(count, (matter.matter_id,)).fetchone()[0] == 0


def test_grants_held_by_the_inactive_principal_are_refused(workbench):
    _client, bench, matter, _runtime = workbench
    repo = bench.workspace.entity_repository(automatic=True)
    with repo.transaction(matter.matter_id, AUTOMATIC_DISCOVERY_PRINCIPAL):
        pass
    columns = [row[1] for row in bench.workspace.connection.execute("PRAGMA table_info(workbench_matter_membership)")]
    values = {"matter_id": matter.matter_id, "principal_id": AUTOMATIC_DISCOVERY_PRINCIPAL, "role": "member",
              "state": "active", "granted_by": WEB_ACTOR, "created_at": "2026-01-01T00:00:00Z",
              "updated_at": "2026-01-01T00:00:00Z", "revoked_at": None}
    with bench.workspace._lock, bench.workspace.connection:
        bench.workspace.connection.execute(
            f"INSERT INTO workbench_matter_membership({','.join(columns)}) VALUES ({','.join('?' * len(columns))})",
            [values.get(column) for column in columns])
    with pytest.raises(KeyError):
        with repo.transaction(matter.matter_id, AUTOMATIC_DISCOVERY_PRINCIPAL):
            pass


def test_a_refused_principal_is_reported_not_ignored(workbench):
    client, bench, matter, _runtime = workbench
    upload(client, matter.slug, "Synthetic memo.txt", FIRST)
    with bench.workspace._lock, bench.workspace.connection:
        bench.workspace.connection.execute(
            "INSERT INTO workbench_principal(principal_id,provider,provider_subject,display_name,login_name,"
            "active,created_at,last_seen_at) VALUES (?,?,?,?,?,1,?,?)",
            (AUTOMATIC_DISCOVERY_PRINCIPAL, "local", "impostor", "Impostor", "impostor",
             "1970-01-01T00:00:00Z", "1970-01-01T00:00:00Z"))
    result = bench.automatic_discovery_pass()
    assert result.failed_matters == (matter.matter_id,)
    assert [event.action for event in bench.workspace.audit_events(matter.matter_id)
            if event.action == "entity.discovery_automatic"] == ["entity.discovery_automatic"]


def test_each_matter_gets_its_own_unit_allowance(workbench):
    client, bench, matter, _runtime = workbench
    other = bench.matter(_create_matter(client, "Synthetic second matter"), WEB_ACTOR)
    upload(client, matter.slug, "Synthetic long log.txt", long_text(30, "Alex"))
    upload(client, other.slug, "Synthetic long log.txt", long_text(30, "Jordan"))
    result = bench.automatic_discovery_pass(unit_limit=10)
    assert result.units == 20  # Ten units in each matter, not twenty in the first.


def test_a_new_extractor_version_replaces_the_old_inventory(workbench):
    client, bench, matter, _runtime = workbench
    upload(client, matter.slug, "Synthetic memo.txt", FIRST)
    bench.run_automatic_discovery_once()
    discovery = bench.automatic_entity_discovery(matter)
    discovery.extractor.version = "synthetic-extractor-v2"
    assert discovery.automatic_step(matter.matter_id) == (1, 1, 0)
    versions = {row[0] for row in bench.workspace.connection.execute(
        "SELECT DISTINCT extractor_version FROM workbench_entity_auto_discovery_unit WHERE matter_id=?",
        (matter.matter_id,))}
    assert versions == {"synthetic-extractor-v2"}
    assert len(names(bench, matter)) == 4  # The same passages are not suggested again.


def test_removed_sources_are_pruned_once_per_matter_pass(workbench, monkeypatch):
    from case_intelligence.entity_repository import EntityRepository
    client, bench, matter, _runtime = workbench
    upload(client, matter.slug, "Synthetic long log.txt", long_text(30, "Alex"))
    calls = []
    original = EntityRepository.prune_auto_discovery
    def counting(self, matter_id):
        calls.append(matter_id)
        return original(self, matter_id)
    monkeypatch.setattr(EntityRepository, "prune_auto_discovery", counting)
    assert bench.run_automatic_discovery_once() == 30  # Two 25/5-unit steps in one pass.
    assert calls == [matter.matter_id]


def ledger_states(bench, matter):
    return [row[0] for row in bench.workspace.connection.execute(
        "SELECT state FROM workbench_entity_auto_discovery_unit WHERE matter_id=? AND unit_ordinal>0",
        (matter.matter_id,))]


def stop_after(checks):
    calls = []
    def should_stop():
        calls.append(None)
        return len(calls) > checks
    return should_stop


def test_a_stop_request_while_inventorying_leaves_the_source_unsealed(workbench):
    client, bench, matter, _runtime = workbench
    upload(client, matter.slug, "Synthetic long log.txt", long_text(30, "Alex"))
    # Checked before the matter, the step and the source, then for every unit read.
    result = bench.automatic_discovery_pass(should_stop=stop_after(5))
    assert result.units == 0 and not result.failed_matters
    assert ledger_states(bench, matter) == []  # Nothing sealed; the next pass starts over.
    assert bench.run_automatic_discovery_once() == 30


def test_a_stop_request_while_loading_pending_units_ends_the_pass(workbench):
    client, bench, matter, _runtime = workbench
    upload(client, matter.slug, "Synthetic long log.txt", long_text(30, "Alex"))
    # 3 checks before the source, 30 while inventorying, then 2 pending units read.
    result = bench.automatic_discovery_pass(should_stop=stop_after(35))
    # The source was inventoried, then the pass stopped instead of reading every unit.
    assert result.units == 0 and not result.failed_matters
    assert set(ledger_states(bench, matter)) == {"pending"}
    assert bench.run_automatic_discovery_once() == 30


def test_source_readers_observe_a_stop_mid_read(workbench):
    import threading
    from case_intelligence.entity_discovery import DiscoveryStopped
    from case_intelligence.entity_unit_reader import EntityUnitReader
    client, bench, matter, _runtime = workbench
    upload(client, matter.slug, "Synthetic long log.txt", long_text(30, "Alex"))
    stop = threading.Event()
    stop.set()
    discovery = bench.automatic_entity_discovery(matter, should_stop=stop.is_set)
    # Only the reader polls here: the inventory read is abandoned before its first
    # unit, and the source stays unsealed instead of being marked failed.
    assert discovery.automatic_step(matter.matter_id) == (0, 0, 0)
    assert ledger_states(bench, matter) == []
    assert bench.workspace.connection.execute(
        "SELECT COUNT(*) FROM workbench_entity_auto_discovery_unit WHERE matter_id=?",
        (matter.matter_id,)).fetchone()[0] == 0
    # The transient index build for pending units is interruptible too and caches nothing.
    store = bench.source_store(matter)
    document = store.get(bench.workspace.connection.execute(
        "SELECT document_id FROM workbench_source_catalog WHERE matter_id=?",
        (matter.matter_id,)).fetchone()[0])
    assert document.units_file
    reader = EntityUnitReader()
    def read_check(_chars):
        raise DiscoveryStopped()
    with pytest.raises(DiscoveryStopped):
        list(reader.iter_selected(store, document, [1], read_check=read_check))
    assert not reader._indexes
    assert len(list(reader.iter_selected(store, document, [1, 30]))) == 2
    # With the index cached, reading a selected record still polls before every bounded read.
    assert reader._indexes
    polls = []
    def stop_on_record_read(chars):
        polls.append(chars)
        raise DiscoveryStopped()
    with pytest.raises(DiscoveryStopped):
        list(reader.iter_selected(store, document, [30], read_check=stop_on_record_read))
    assert polls == [0]
    # A stop that arrives after the last read is observed before the record is decoded.
    calls = []
    def stop_before_decode(chars):
        calls.append(chars)
        if len(calls) == 3:
            raise DiscoveryStopped()
    with pytest.raises(DiscoveryStopped):
        list(reader.iter_selected(store, document, [30], read_check=stop_before_decode))
    # Before the buffer, before its one read (a small record), then before decoding.
    assert calls[:2] == [0, 0] and calls[2] > 0
    stop.clear()
    assert bench.run_automatic_discovery_once() == 30


def test_selected_records_are_read_into_one_buffer_with_a_poll_per_read():
    import io
    import json
    from case_intelligence.entity_unit_reader import READ_BYTES, EntityUnitReader
    record = json.dumps({"text": "Synthetic " * 30000}).encode()
    assert len(record) > 4 * READ_BYTES
    polls = []
    serialized = EntityUnitReader._read_span(io.BytesIO(record), len(record), polls.append)
    assert isinstance(serialized, bytearray) and bytes(serialized) == record
    # Once before the buffer is reserved, then before every bounded read.
    assert polls == [0] + [index * READ_BYTES for index in range(-(-len(record) // READ_BYTES))]
    with pytest.raises(ValueError):
        EntityUnitReader._read_span(io.BytesIO(record[:-1]), len(record), lambda chars: None)
    # A stop already requested is observed before the record's buffer is reserved.
    import tracemalloc
    from case_intelligence.entity_discovery import DiscoveryStopped
    def stop(_chars):
        raise DiscoveryStopped()
    tracemalloc.start()
    try:
        with pytest.raises(DiscoveryStopped):
            EntityUnitReader._read_span(io.BytesIO(b""), 64 * 1024 * 1024, stop)
        assert tracemalloc.get_traced_memory()[1] < 1024 * 1024
    finally:
        tracemalloc.stop()


def test_selected_record_decoding_holds_one_serialization_at_a_time(workbench):
    import tracemalloc
    from case_intelligence.entity_unit_reader import EntityUnitReader
    client, bench, matter, _runtime = workbench
    # One large synthetic unit: twenty long lines, about 1.8 MB in a single record.
    upload(client, matter.slug, "Synthetic long memo.txt",
           "\n".join("Alex Example arrived. " * 4_000 for _ in range(20)).encode())
    store = bench.source_store(matter)
    document = store.get(bench.workspace.connection.execute(
        "SELECT document_id FROM workbench_source_catalog WHERE matter_id=?", (matter.matter_id,)).fetchone()[0])
    reader = EntityUnitReader()
    list(reader.iter_selected(store, document, [1]))  # Build and cache the index first.
    ((start, end),) = next(iter(reader._indexes.values()))
    span = end - start
    assert span > 1_000_000
    tracemalloc.start()
    try:
        ((ordinal, unit),) = list(reader.iter_selected(store, document, [1], read_check=lambda chars: None))
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert ordinal == 1 and unit.text
    # Bytes and their decoded text never coexist with the parsed record:
    # roughly two copies at the peak, not three.
    assert peak < 2.5 * span, (peak, span)


KILLED_MID_UNIT = """
import os, sys
from fastapi.testclient import TestClient
from case_intelligence import entity_discovery
from case_intelligence.workbench import create_workbench_app
from tests.test_automatic_discovery import FIRST, upload
from tests.test_matter_notebook import WEB_ACTOR, _create_matter
with TestClient(create_workbench_app(sys.argv[1], auth_mode="test")) as client:
    slug = _create_matter(client)
    upload(client, slug, "Synthetic memo.txt", FIRST)
    bench = client.app.state.workbench
    def killed(repo, unit, state, note):
        if state == "processed":
            # The unit's suggestions are written but not committed: a forced stop
            # after the shutdown grace period lands exactly here.
            count = repo.connection.execute(
                "SELECT COUNT(*) FROM workbench_entity WHERE status='suggested'").fetchone()[0]
            print(count, flush=True)
            os._exit(137)
    entity_discovery.AutomaticLedger.state = staticmethod(killed)
    bench.run_automatic_discovery_once()
"""


def test_a_forced_stop_mid_unit_leaves_the_store_consistent(tmp_path, monkeypatch):
    """A record decode or extraction that outlasts the shutdown grace period ends
    in a forced kill; the unit's transaction rolls back and is redone later."""
    import os
    import subprocess
    import sys
    from pathlib import Path
    monkeypatch.setenv("CASE_INTELLIGENCE_STORAGE_RESERVE_GIB", "0")
    monkeypatch.delenv("CASE_INTELLIGENCE_AUTOMATIC_DISCOVERY", raising=False)
    runtime = tmp_path / "runtime"
    root = Path(__file__).resolve().parents[1]
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(root / "src"), str(root)])}
    killed = subprocess.run([sys.executable, "-c", KILLED_MID_UNIT, str(runtime)], cwd=root, env=env,
                            capture_output=True, text=True, timeout=300)
    assert killed.returncode == 137, killed.stderr
    assert int(killed.stdout.split()[-1]) == 4  # Uncommitted suggestions existed when it was killed.
    with TestClient(create_workbench_app(runtime, auth_mode="test")) as client:
        bench = client.app.state.workbench
        matter_id = bench.workspace.connection.execute("SELECT matter_id FROM workbench_matter").fetchone()[0]
        matter = bench.workspace.get_matter_by_id(matter_id)
        connection = bench.workspace.connection
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert not connection.execute("PRAGMA foreign_key_check").fetchall()
        # Nothing from the interrupted unit persisted: no suggestions, no audit, unit still pending.
        assert names(bench, matter) == []
        assert ledger_states(bench, matter) == ["pending"]
        assert not [event for event in bench.workspace.audit_events(matter_id)
                    if event.action == "entity.discovery_unit"]
        # The next run completes it exactly once.
        assert bench.run_automatic_discovery_once() == 1
        assert len(names(bench, matter)) == 4


def test_closing_waits_for_the_discovery_worker(monkeypatch):
    import threading
    import case_intelligence.workbench as workbench_module
    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    def slow_pass(ids, should_stop=None):
        started.set()
        release.wait(5)
        finished.set()
        return workbench_module.AutomaticDiscoveryPass(0, 0)
    coordinator = AutomaticDiscoveryCoordinator(slow_pass)
    assert started.wait(5)
    threading.Timer(0.2, release.set).start()
    coordinator.close()
    assert finished.is_set()  # close() returned only after the pass finished.
