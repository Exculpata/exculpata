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

        # Cross-matter isolation: another matter never sees these Bates labels.
        other = review.bench.create_matter("isolated-matter", "Isolated Matter", ACTOR)
        assert repo.document_bates_ranges(other.matter_id, [memo.document_id]) == {}
        assert not re.search(r"PROD0001", review.client.get(f"/matters/{other.slug}").text)
