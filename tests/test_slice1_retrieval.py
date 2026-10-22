from __future__ import annotations


from case_intelligence.legacy_names import (
    LEGACY_STEM,
)
from dataclasses import asdict, replace

import pytest

from case_intelligence.jobs import JobService
from case_intelligence.retrieval import (
    RetrievalIntegrityError,
    RetrievalService,
    SearchRequest,
    SourceUnavailable,
)
from case_intelligence.tickets import TicketStore
from tests.slice1_support import NOW, make_environment


def ready(tmp_path):
    store, registry, inventory, access, roots = make_environment(tmp_path)
    for name in ("alpha", "bravo"):
        inventory.inventory(f"matter-{name}", f"location-{name}", f"scan-{name}")
        JobService(store, registry, clock=lambda: NOW).run_next(f"matter-{name}")
    tickets = TicketStore(registry=registry, clock=lambda: NOW,
                          token_source=lambda size: b"x" * size)
    return store, registry, inventory, access, roots, tickets


def test_exact_and_lexical_rrf_are_matter_filtered_and_search_mints_no_ticket(tmp_path):
    store, registry, _, access, _, tickets = ready(tmp_path)
    service = RetrievalService(store, registry, access, tickets)
    exact = service.search("actor-alpha", SearchRequest("run-exact", "matter-alpha", "CITRINE-FALCON-731"))
    lexical = service.search("actor-alpha", SearchRequest("run-lexical", "matter-alpha", "shared red bicycle"))
    bravo = service.search("actor-bravo", SearchRequest("run-bravo", "matter-bravo", "shared red bicycle"))
    assert exact.outcome == lexical.outcome == bravo.outcome == "matched"
    assert exact.results[0].source_name == "report.txt"
    assert "CITRINE" in lexical.results[0].excerpt and "VIOLET" in bravo.results[0].excerpt
    assert tickets.records == ()
    assert service.search("actor-alpha", SearchRequest("none", "matter-alpha", "absent phrase")).outcome == "no_match"
    store.close()


def test_unauthorized_fails_before_lookup_or_scoring_and_mixed_candidates_reject(tmp_path):
    store, registry, _, access, _, tickets = ready(tmp_path)
    service = RetrievalService(store, registry, access, tickets)
    with pytest.raises(PermissionError):
        service.search("actor-bravo", SearchRequest("bad", "matter-alpha", "shared red bicycle"))
    assert (service.lookup_count, service.scoring_count) == (0, 0)
    base = tuple(service._current("matter-alpha"))[0]
    mixed = RetrievalService(store, registry, access, tickets,
                             candidate_provider=lambda matter: (base, replace(base, matter_id="matter-bravo")))
    with pytest.raises(PermissionError):
        mixed.search("actor-alpha", SearchRequest("mixed", "matter-alpha", "shared red bicycle"))
    store.close()


def test_exact_citation_tamper_fails_whole_run_and_projection_is_safe(tmp_path):
    store, registry, _, access, _, tickets = ready(tmp_path)
    service = RetrievalService(store, registry, access, tickets)
    response = service.search("actor-alpha", SearchRequest("safe", "matter-alpha", "shared red bicycle"))
    projected = asdict(response)
    flattened = repr(projected).casefold()
    for forbidden in ("relative_path", "sha256", "synthetic_root", "unc", "rrf", "score", "processor", "lane", "traceback"):
        assert forbidden not in flattened
    with store.transaction():
        # fixture-write: Isolated SQLiteStore citation tampering uses its public explicit transaction in native legacy-autocommit mode.
        store.connection.execute("UPDATE segment SET text=text||'tampered' WHERE matter_id='matter-alpha'")
    with pytest.raises(RetrievalIntegrityError):
        service.search("actor-alpha", SearchRequest("tampered", "matter-alpha", "shared red bicycle"))
    store.close()


def test_explicit_selection_fresh_validates_then_mints_opaque_ticket(tmp_path):
    store, registry, inventory, access, roots, tickets = ready(tmp_path)
    service = RetrievalService(store, registry, access, tickets)
    result = service.search("actor-alpha", SearchRequest("select", "matter-alpha", "shared red bicycle")).results[0]
    launch = service.mint_open_source("actor-alpha", "matter-alpha", result.segment_navigation_id)
    assert launch.model_dump().keys() == {"uri", "expires_at"}
    assert launch.uri.startswith(f"{LEGACY_STEM}://ticket/")
    assert "matter" not in launch.uri and "report" not in launch.uri
    assert len(tickets.records) == 1

    report = roots["alpha"] / "nested/report.txt"
    report.write_bytes(report.read_bytes() + b"changed\n")
    inventory.inventory("matter-alpha", "location-alpha", "changed")
    with pytest.raises(SourceUnavailable, match="source_unavailable"):
        service.mint_open_source("actor-alpha", "matter-alpha", result.segment_navigation_id)
    with pytest.raises(PermissionError):
        service.mint_open_source("actor-bravo", "matter-alpha", result.segment_navigation_id)
    store.close()
