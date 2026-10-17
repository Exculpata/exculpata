from __future__ import annotations

from tests.fixture_write import fixture_write

import sqlite3
from dataclasses import asdict, replace

import pytest

from case_intelligence.contracts import Matter, MatterState, SourceReference
from case_intelligence.inventory import InventoryService
from case_intelligence.isolation import MatterStateRegistry, ReferenceResolutionStatus
from case_intelligence.jobs import JobService
from case_intelligence.retrieval import RetrievalService, SearchRequest, SourceUnavailable
from case_intelligence.sqlite_store import SQLiteStore
from case_intelligence.text_records import PlainTextRecord, resolve_persisted_reference
from case_intelligence.tickets import TicketStore
from tests.slice1_support import NOW, make_environment


def _ready(tmp_path, token_hook=None):
    store, registry, inventory, access, roots = make_environment(tmp_path)
    for name in ("alpha", "bravo"):
        inventory.inventory(f"matter-{name}", f"location-{name}", f"scan-{name}")
        JobService(store, registry, clock=lambda: NOW).run_next(f"matter-{name}")
    tickets = TicketStore(registry=registry, clock=lambda: NOW,
        token_source=token_hook or (lambda size: b"z" * size))
    return store, registry, inventory, access, roots, tickets


@pytest.mark.parametrize("stage", ["after_observation", "after_missing", "after_cas"])
def test_apply_faults_rollback_catalog_but_preserve_failed_attempt(tmp_path, stage):
    store, registry, base, _, _ = make_environment(tmp_path)
    snapshot = base.prepare_snapshot("matter-alpha", "location-alpha", f"fault-{stage}")
    def fail(observed):
        if observed == stage:
            raise RuntimeError("injected")
    service = InventoryService(store, registry, clock=lambda: NOW, _test_hook=fail)
    with pytest.raises(RuntimeError, match="injected"):
        service.apply_snapshot(snapshot)
    assert store.connection.execute("SELECT count(*) FROM source_file").fetchone()[0] == 0
    assert store.connection.execute("SELECT state FROM scan_run").fetchone()[0] == "failed"
    assert store.connection.execute("SELECT state FROM scan_attempt").fetchone()[0] == "failed"
    store.close()


def test_superseded_attempt_is_durable_across_reopen(tmp_path):
    store_a, registry, inv_a, _, roots = make_environment(tmp_path)
    stale = inv_a.prepare_snapshot("matter-alpha", "location-alpha", "stale")
    (roots["alpha"] / "nested/report.txt").write_text("newer bytes", encoding="utf-8")
    store_b = SQLiteStore(store_a.path)
    inv_b = InventoryService(store_b, registry, clock=lambda: NOW)
    inv_b.apply_snapshot(inv_b.prepare_snapshot("matter-alpha", "location-alpha", "fresh"))
    assert inv_a.apply_snapshot(stale).state == "superseded"
    store_b.close(); store_a.close()
    reopened = SQLiteStore(tmp_path / "catalog.sqlite")
    row = reopened.connection.execute("SELECT state,failure_category FROM scan_attempt WHERE state='superseded'").fetchone()
    assert tuple(row) == ("superseded", "catalog_revision_conflict")
    reopened.close()


def test_lock_order_registry_then_sqlite_then_ticket(tmp_path):
    observations = []
    holder = {}
    def token(size):
        observations.append((holder["registry"]._lock._is_owned(), holder["store"].connection.in_transaction))
        return b"q" * size
    store, registry, _, access, _, tickets = _ready(tmp_path, token)
    holder.update(store=store, registry=registry)
    service = RetrievalService(store, registry, access, tickets)
    result = service.search("actor-alpha", SearchRequest("lock", "matter-alpha", "shared red bicycle")).results[0]
    service.mint_open_source("actor-alpha", "matter-alpha", result.segment_navigation_id)
    assert observations == [(True, False)]
    store.close()


@pytest.mark.parametrize("change", ["delete", "rewrite"])
def test_source_disappearance_or_change_after_search_mints_no_ticket(tmp_path, change):
    store, registry, _, access, roots, tickets = _ready(tmp_path)
    service = RetrievalService(store, registry, access, tickets)
    result = service.search("actor-alpha", SearchRequest("fresh", "matter-alpha", "shared red bicycle")).results[0]
    path = roots["alpha"] / "nested/report.txt"
    path.unlink() if change == "delete" else path.write_text("different", encoding="utf-8")
    with pytest.raises(SourceUnavailable, match="source_unavailable"):
        service.mint_open_source("actor-alpha", "matter-alpha", result.segment_navigation_id)
    assert tickets.records == ()
    store.close()


def test_cross_matter_injection_rejects_at_schema_lane_citation_projection_and_ticket(tmp_path):
    store, registry, _, access, _, tickets = _ready(tmp_path)
    alpha = store.connection.execute("SELECT * FROM segment WHERE matter_id='matter-alpha'").fetchone()
    bravo_version = store.connection.execute("SELECT source_version_id FROM source_version WHERE matter_id='matter-bravo' AND media_type='text/plain'").fetchone()[0]
    with pytest.raises(sqlite3.IntegrityError):
        with fixture_write(store):
            store.connection.execute("UPDATE segment SET source_version_id=? WHERE segment_id=?", (bravo_version, alpha["segment_id"]))
    service = RetrievalService(store, registry, access, tickets)
    base = tuple(service._current("matter-alpha"))[0]
    mixed = RetrievalService(store, registry, access, tickets,
        candidate_provider=lambda matter: (base, replace(base, matter_id="matter-bravo")))
    with pytest.raises(PermissionError):
        mixed.search("actor-alpha", SearchRequest("mixed", "matter-alpha", "shared red bicycle"))
    response = service.search("actor-alpha", SearchRequest("safe", "matter-alpha", "shared red bicycle"))
    projection = asdict(response)
    assert projection["matter_id"] == "matter-alpha"
    with pytest.raises(PermissionError):
        service.mint_open_source("actor-alpha", "matter-alpha",
            service.search("actor-bravo", SearchRequest("b", "matter-bravo", "shared red bicycle")).results[0].segment_navigation_id)
    store.close()


def test_persisted_resolver_precedence_missing_before_stale_and_never_falls_forward(tmp_path):
    store, registry, inventory, access, roots, tickets = _ready(tmp_path)
    service = RetrievalService(store, registry, access, tickets)
    candidate = tuple(service._current("matter-alpha"))[0]
    source_file, version = service._load_source("matter-alpha", candidate.source_file_id, candidate.source_version_id)
    record = PlainTextRecord(candidate.segment_id, candidate.representation_id, candidate.matter_id,
        candidate.source_file_id, candidate.source_version_id, candidate.text, candidate.text_sha256,
        candidate.character_start, candidate.character_end, candidate.line_start, candidate.line_end,
        candidate.exact_keys, __import__('case_intelligence.contracts', fromlist=['SourceLocator']).SourceLocator(
            representation_id=candidate.representation_id, segment_id=candidate.segment_id,
            character_start=candidate.character_start, character_end=candidate.character_end,
            line_start=candidate.line_start, line_end=candidate.line_end, excerpt_sha256=candidate.text_sha256))
    reference = SourceReference(reference_id="ref", matter_id="matter-alpha",
        source_version_id=version.source_version_id, locator=record.locator)
    (roots["alpha"] / "nested/report.txt").write_text("new bytes", encoding="utf-8")
    inventory.inventory("matter-alpha", "location-alpha", "changed")
    current_file, _ = service._load_source("matter-alpha", candidate.source_file_id, candidate.source_version_id)
    assert resolve_persisted_reference("matter-alpha", reference, current_file, version, record=record,
        excerpt_sha256=record.text_sha256).status is ReferenceResolutionStatus.STALE
    (roots["alpha"] / "nested/report.txt").unlink()
    inventory.inventory("matter-alpha", "location-alpha", "missing")
    missing_file, _ = service._load_source("matter-alpha", candidate.source_file_id, candidate.source_version_id)
    assert resolve_persisted_reference("matter-alpha", reference, missing_file, version, record=record,
        excerpt_sha256=record.text_sha256).status is ReferenceResolutionStatus.UNAVAILABLE
    store.close()
