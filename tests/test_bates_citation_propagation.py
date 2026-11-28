"""Synthetic regressions for propagating produced Bates and team IDs into citations."""
from __future__ import annotations

import re

from case_intelligence.modules import ModuleDatabase
from case_intelligence.modules.discovery_review.identifiers import IdentifierRepository
from case_intelligence.modules.discovery_review.production import ProductionRepository
from tests.test_discovery_review_dat_opt import ACTOR, complete_listing, dat_bytes, opened_review
from tests.test_discovery_review_image_load_files import upload_image_production


def test_bates_and_team_id_propagate_into_citations_without_altering_tokens(tmp_path, monkeypatch):
    monkeypatch.setenv("CASE_INTELLIGENCE_STORAGE_RESERVE_GIB", "0")
    monkeypatch.setenv("CASE_INTELLIGENCE_OCR_MODE", "disabled")
    dat = dat_bytes(
        ("BEGDOC", "ENDDOC", "NATIVEPATH"),
        [
            ("PROD0001", "PROD0003", "NATIVES\\memo.txt"),
            ("PROD0010", "PROD0010", "NATIVES\\single.txt"),
        ],
    )
    with opened_review(tmp_path / "runtime") as review:
        upload_image_production(review, {
            "Vol1/NATIVES/memo.txt": b"Synthetic witness memo mentioning harbor shipment.\n",
            "Vol1/NATIVES/single.txt": b"Synthetic single page ledger entry.\n",
            "Vol1/NATIVES/unlabeled.txt": b"Synthetic unlabeled note mentioning harbor.\n",
            "Vol1/DATA/records.dat": dat,
        })
        # Complete the stored production import so DAT associations are persisted.
        complete_listing(review)

        memo = review.document("Vol1/NATIVES/memo.txt")
        single = review.document("Vol1/NATIVES/single.txt")
        unlabeled = review.document("Vol1/NATIVES/unlabeled.txt")

        # Assign a Team ID to the unlabeled document only.
        identifiers = IdentifierRepository(
            ModuleDatabase(review.bench.workspace.connection, review.bench.workspace._lock),
            review.bench.workspace.membership,
        )
        identifiers.assign(review.matter.matter_id, ACTOR, [(unlabeled.document_id, "TEAM0001")])

        repo = ProductionRepository(ModuleDatabase(review.bench.workspace.connection, review.bench.workspace._lock))
        ranges = repo.document_bates_ranges(review.matter.matter_id, [memo.document_id, single.document_id, unlabeled.document_id])
        assert ranges == {
            memo.document_id: "PROD0001–PROD0003",
            single.document_id: "PROD0010",
        }

        # Citations from bench.search carry exact produced Bates or Team ID without changing support_token.
        hits = review.bench.search(review.matter, "harbor", limit=10)
        by_doc = {hit.document_id: hit for hit in hits}
        assert by_doc[memo.document_id].bates_label == "Bates: PROD0001–PROD0003"
        assert by_doc[memo.document_id].source_name == memo.display_name
        assert by_doc[unlabeled.document_id].bates_label == "Team ID: TEAM0001"

        # Serialized payloads carry bates_label when present and round-trip cleanly.
        memo_payload = review.bench._workflow_citation_payload(by_doc[memo.document_id])
        assert memo_payload["bates_label"] == "Bates: PROD0001–PROD0003"
        assert by_doc[memo.document_id].staff_payload()["bates_label"] == "Bates: PROD0001–PROD0003"
        restored = review.bench._workflow_citation(memo_payload)
        assert restored == by_doc[memo.document_id]

        # Exact search HTML workspace renders the Bates label on citation chips.
        search_page = review.client.get(f"/matters/{review.matter.slug}", params={"mode": "search", "q": "harbor"})
        assert search_page.status_code == 200
        assert "Bates: PROD0001–PROD0003" in search_page.text
        assert "Team ID: TEAM0001" in search_page.text

        # Full-text locator identity excludes display-only bates_label while hydration preserves live label.
        from case_intelligence.full_text_review import compact_locator, resolve_text_report_citations, validate_text_citations
        unit = next(iter(memo.iter_parsed_units()))
        legacy_locator = compact_locator({k: v for k, v in memo_payload.items() if k != "bates_label"}, 1, unit.text)
        assert "bates_label" not in compact_locator(memo_payload, 1, unit.text)
        validate_text_citations(review.bench, review.matter, [legacy_locator])
        hydrated = resolve_text_report_citations(review.bench, review.matter, [legacy_locator])
        assert hydrated[0]["bates_label"] == "Bates: PROD0001–PROD0003"

        # Shared assistant fragment renders bates_label on saved claim citations.
        conv = review.bench.workspace.create_conversation(review.matter.matter_id, "Bates Check", actor_id=ACTOR)
        saved_cite = review.bench._saved_answer_citation_payload(by_doc[memo.document_id])
        review.bench.workspace.append_message(
            review.matter.matter_id, conv.conversation_id, "assistant", "Harbor note found.",
            payload={"kind": "generated", "answerable": True, "claims": [{"text": "Harbor note.", "citations": [saved_cite]}]},
        )
        assistant_page = review.client.get(f"/matters/{review.matter.slug}/assistant", params={"conversation": conv.conversation_id})
        assert assistant_page.status_code == 200 and "Bates: PROD0001–PROD0003" in assistant_page.text

        # Removing the DAT load file immediately stops publishing stale produced Bates labels.
        dat_token = review.token("Vol1/DATA/records.dat")
        removed = review.client.post(f"/matters/{review.matter.slug}/sources/{dat_token}/remove", follow_redirects=False)
        assert removed.status_code in {200, 303}
        assert repo.document_bates_ranges(review.matter.matter_id, [memo.document_id]) == {}
        after_hits = {hit.document_id: hit for hit in review.bench.search(review.matter, "harbor", limit=10)}
        assert after_hits[memo.document_id].bates_label == ""

        # Cross-matter isolation: another matter never sees these Bates labels.
        other = review.bench.create_matter("isolated-matter", "Isolated Matter", ACTOR)
        assert repo.document_bates_ranges(other.matter_id, [memo.document_id]) == {}
        assert not re.search(r"PROD0001", review.client.get(f"/matters/{other.slug}").text)


def test_cold_reopen_and_companion_mutation_withhold_stale_bates_until_reindexed(tmp_path, monkeypatch):
    from case_intelligence.case_assistant_tools import MatterToolRuntime
    from case_intelligence.workbench_assistant import WorkbenchToolBackend

    monkeypatch.setenv("CASE_INTELLIGENCE_STORAGE_RESERVE_GIB", "0")
    monkeypatch.setenv("CASE_INTELLIGENCE_OCR_MODE", "disabled")
    runtime = tmp_path / "runtime"
    dat = dat_bytes(
        ("BEGDOC", "ENDDOC", "NATIVEPATH"),
        [
            ("PROD0001", "PROD0003", "NATIVES\\memo.txt"),
            ("PROD0004", "PROD0004", "NATIVES\\companion.txt"),
        ],
    )
    with opened_review(runtime) as review:
        upload_image_production(review, {
            "Vol1/NATIVES/memo.txt": b"Synthetic witness memo mentioning harbor dock.\n",
            "Vol1/NATIVES/companion.txt": b"Synthetic companion exhibit mentioning dock.\n",
            "Vol1/DATA/records.dat": dat,
        })
        complete_listing(review)
        slug = review.matter.slug
        memo_id = review.document("Vol1/NATIVES/memo.txt").document_id
        repo = ProductionRepository(ModuleDatabase(review.bench.workspace.connection, review.bench.workspace._lock))
        assert repo.document_bates_ranges(review.matter.matter_id, [memo_id], store=review.store) == {memo_id: "PROD0001\u2013PROD0003"}

    # Cold matter reopen before ProductionService re-validation must withhold stale Bates ranges.
    with opened_review(runtime, slug) as reopened:
        reopened_repo = ProductionRepository(ModuleDatabase(reopened.bench.workspace.connection, reopened.bench.workspace._lock))
        assert reopened_repo.document_bates_ranges(reopened.matter.matter_id, [memo_id], store=reopened.store) == {}
        cold_hits = {hit.document_id: hit for hit in reopened.bench.search(reopened.matter, "harbor", limit=10)}
        assert cold_hits[memo_id].bates_label == ""
        cold_runtime = MatterToolRuntime(reopened.matter.matter_id, ACTOR, WorkbenchToolBackend(reopened.bench, ACTOR))
        cold_sources = {row["document_id"]: row for row in cold_runtime.invoke("list_sources", {"limit": 20})["sources"]}
        assert cold_sources[memo_id]["bates"] == ""

        # Re-indexing restores fresh Bates labels across repository, search citations, and Case Assistant.
        complete_listing(reopened)
        assert reopened_repo.document_bates_ranges(reopened.matter.matter_id, [memo_id], store=reopened.store) == {memo_id: "PROD0001\u2013PROD0003"}
        warm_hits = {hit.document_id: hit for hit in reopened.bench.search(reopened.matter, "harbor", limit=10)}
        assert warm_hits[memo_id].bates_label == "Bates: PROD0001\u2013PROD0003"
        warm_sources = {row["document_id"]: row for row in cold_runtime.invoke("list_sources", {"limit": 20})["sources"]}
        assert warm_sources[memo_id]["bates"] == "PROD0001\u2013PROD0003"

        # Mutating a companion file in the same production root outside the load file invalidates cached Bates until re-indexed.
        companion_token = reopened.token("Vol1/NATIVES/companion.txt")
        removed = reopened.client.post(f"/matters/{slug}/sources/{companion_token}/remove", follow_redirects=False)
        assert removed.status_code in {200, 303}
        assert reopened_repo.document_bates_ranges(reopened.matter.matter_id, [memo_id], store=reopened.store) == {}
        mutated_hits = {hit.document_id: hit for hit in reopened.bench.search(reopened.matter, "harbor", limit=10)}
        assert mutated_hits[memo_id].bates_label == ""
        mutated_sources = {row["document_id"]: row for row in cold_runtime.invoke("list_sources", {"limit": 20})["sources"]}
        assert mutated_sources[memo_id]["bates"] == ""

        # Re-indexing after the companion removal restores validated Bates for the remaining matched memo.
        complete_listing(reopened)
        assert reopened_repo.document_bates_ranges(reopened.matter.matter_id, [memo_id], store=reopened.store) == {memo_id: "PROD0001\u2013PROD0003"}


def test_bates_freshness_acquires_store_before_db_and_reuses_revision_cache(tmp_path, monkeypatch):
    from contextlib import contextmanager

    monkeypatch.setenv("CASE_INTELLIGENCE_STORAGE_RESERVE_GIB", "0")
    monkeypatch.setenv("CASE_INTELLIGENCE_OCR_MODE", "disabled")
    dat = dat_bytes(
        ("BEGDOC", "ENDDOC", "NATIVEPATH"),
        [
            ("PROD0001", "PROD0002", "NATIVES\\memo_a.txt"),
            ("PROD0003", "PROD0004", "NATIVES\\memo_b.txt"),
        ],
    )
    with opened_review(tmp_path / "runtime") as review:
        upload_image_production(review, {
            "Vol1/NATIVES/memo_a.txt": b"Harbor alpha synthetic statement.\n",
            "Vol1/NATIVES/memo_b.txt": b"Harbor beta synthetic statement.\n",
            "Vol1/DATA/records.dat": dat,
        })
        complete_listing(review)
        memo_a_id = review.document("Vol1/NATIVES/memo_a.txt").document_id
        memo_b_id = review.document("Vol1/NATIVES/memo_b.txt").document_id

        events: list[str] = []
        raw_db_lock = review.bench.workspace._lock
        raw_guard = review.store.mutation_guard

        class _TrackedDbLock:
            def __enter__(self):
                events.append("db_enter")
                raw_db_lock.__enter__()
                return self

            def __exit__(self, exc_type, exc, tb):
                events.append("db_exit")
                return raw_db_lock.__exit__(exc_type, exc, tb)

        @contextmanager
        def _tracked_guard():
            events.append("store_enter")
            with raw_guard():
                yield
            events.append("store_exit")

        monkeypatch.setattr(review.store, "mutation_guard", _tracked_guard)
        repo = ProductionRepository(ModuleDatabase(review.bench.workspace.connection, _TrackedDbLock()))

        read_calls: list[list[str]] = []
        raw_read = review.store.source_changes.read

        def _tracked_read(doc_ids):
            read_calls.append(list(doc_ids))
            return raw_read(doc_ids)

        monkeypatch.setattr(review.store.source_changes, "read", _tracked_read)

        # First call validates freshness once (batched across candidates) with store acquired before db.
        assert repo.document_bates_ranges(review.matter.matter_id, [memo_a_id], store=review.store) == {memo_a_id: "PROD0001\u2013PROD0002"}
        assert events[:2] == ["store_enter", "db_enter"]
        assert len(read_calls) == 1

        # Subsequent citation reads at the same store revision reuse the validated freshness result.
        assert repo.document_bates_ranges(review.matter.matter_id, [memo_b_id], store=review.store) == {memo_b_id: "PROD0003\u2013PROD0004"}
        hits = {hit.document_id: hit for hit in review.bench.search(review.matter, "harbor", limit=10)}
        assert hits[memo_a_id].bates_label == "Bates: PROD0001\u2013PROD0002"
        assert hits[memo_b_id].bates_label == "Bates: PROD0003\u2013PROD0004"
        assert len(read_calls) == 1


