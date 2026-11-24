"""Synthetic proving and fail-closed tests for Case Assistant production and Bates tools."""
from __future__ import annotations

import pytest

from case_intelligence.case_assistant import validate_assistant_tool_payload
from case_intelligence.case_assistant_tools import (
    ALLOWED_TOOLS, AssistantAccessError, AssistantToolError, MatterToolRuntime, openai_tool_schemas,
)
from case_intelligence.modules import ModuleDatabase
from case_intelligence.modules.discovery_review.identifiers import IdentifierRepository
from case_intelligence.workbench_assistant import WorkbenchToolBackend
from tests.test_discovery_review_dat_opt import ACTOR, complete_listing, dat_bytes, opened_review
from tests.test_workflow_quality_handoff import pdf_bytes


def _upload_volume(review, files, media_overrides=None):
    import time
    import uuid
    from pathlib import Path
    from tests.test_intake_receipt_http import put, selection, upload

    media = {".pdf": "application/pdf", ".csv": "text/csv", ".dat": "text/plain", ".txt": "text/plain"}
    overrides = media_overrides or {}
    names = list(files)
    descriptors = [{"name": Path(n).name, "relative_path": n, "size": len(files[n]),
                    "media_type": overrides.get(n, media.get(Path(n).suffix.lower(), "text/plain"))} for n in names]
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
            break
        time.sleep(0.01)


def test_inspect_production_schema_and_allowlist():
    assert "inspect_production" in ALLOWED_TOOLS
    schemas = openai_tool_schemas()
    names = {entry["function"]["name"] for entry in schemas}
    assert "inspect_production" in names
    validate_assistant_tool_payload({"tools": schemas, "messages": [{"role": "user", "content": "Check production"}]})


def test_list_sources_bates_and_inspect_production_end_to_end(tmp_path, monkeypatch):
    monkeypatch.setenv("CASE_INTELLIGENCE_STORAGE_RESERVE_GIB", "0")
    monkeypatch.setenv("CASE_INTELLIGENCE_OCR_MODE", "disabled")
    dat = dat_bytes(
        ("BEGDOC", "ENDDOC", "NATIVEPATH"),
        [
            ("PROD0001", "PROD0002", "NATIVES\\alpha.pdf"),
            ("PROD0004", "PROD0004", "NATIVES\\beta.pdf"),
        ],
    )
    csv_load = (
        b"path,begin_bates,end_bates\n"
        b"Vol1/NATIVES/alpha.pdf,PROD0001,PROD0002\n"
        b"Vol1/NATIVES/beta.pdf,PROD0004,PROD0004\n"
    )
    with opened_review(tmp_path / "runtime") as review:
        _upload_volume(review, {
            "Vol1/NATIVES/alpha.pdf": pdf_bytes(["Alpha page 1.", "Alpha page 2."]),
            "Vol1/NATIVES/beta.pdf": pdf_bytes(["Beta page 4."]),
            "Vol1/NATIVES/extra.txt": b"Unproduced team note.\n",
            "Vol1/NATIVES/broken.pdf": b"%PDF-1.4\n%%corrupted-stream",
            "Vol1/DATA/records.dat": dat,
            "Vol1/DATA/bates.csv": csv_load,
        })
        complete_listing(review)

        alpha = review.document("Vol1/NATIVES/alpha.pdf")
        extra = review.document("Vol1/NATIVES/extra.txt")
        csv_doc = review.document("Vol1/DATA/bates.csv")

        identifiers = IdentifierRepository(
            ModuleDatabase(review.bench.workspace.connection, review.bench.workspace._lock),
            review.bench.workspace.membership,
        )
        identifiers.assign(review.matter.matter_id, ACTOR, [(extra.document_id, "TEAM0009")])

        backend = WorkbenchToolBackend(review.bench, ACTOR)
        runtime = MatterToolRuntime(review.matter.matter_id, ACTOR, backend)

        # list_sources populates exact produced Bates and distinct Team ID, and filters by Bates query.
        listed = runtime.invoke("list_sources", {"limit": 50})
        by_id = {row["document_id"]: row for row in listed["sources"]}
        assert by_id[alpha.document_id]["bates"] == "PROD0001\u2013PROD0002"
        assert by_id[extra.document_id]["bates"] == "Team ID: TEAM0009"

        bates_search = runtime.invoke("list_sources", {"query": "PROD0001", "limit": 10})
        assert [row["document_id"] for row in bates_search["sources"]] == [alpha.document_id]

        # inspect_production returns counts, failed files, DAT attempt coverage, and Bates-gap findings.
        overview = runtime.invoke("inspect_production", {"load_file_document_id": csv_doc.document_id})
        assert overview["total_sources"] == 6
        assert overview["counts_by_media_type"]["application/pdf"] == 3
        assert overview["recorded_pdf_pages"] == 3
        assert any(item["source_name"].endswith("broken.pdf") for item in overview["unavailable_sources"])
        assert any(att["source_format"] == "dat" and att["status"] == "complete" for att in overview["load_file_attempts"])
        assert overview["bates_gaps"]["notice"]
        assert any("PROD0003" in fact["text"] for fact in overview["bates_gaps"]["facts"])

        # Source-set scope hides out-of-scope documents, suppresses aggregate attempt counts even if DAT is selected, and blocks out-of-scope load_file_document_id.
        dat_doc = review.document("Vol1/DATA/records.dat")
        scoped = MatterToolRuntime(
            review.matter.matter_id, ACTOR, backend, allowed_document_ids=frozenset({alpha.document_id, dat_doc.document_id}),
        )
        scoped_overview = scoped.invoke("inspect_production", {})
        assert scoped_overview["total_sources"] == 2
        assert scoped_overview["load_file_attempts"] == []
        assert scoped_overview["bates_gaps"]["computed"] is False
        with pytest.raises(AssistantAccessError):
            scoped.invoke("inspect_production", {"load_file_document_id": csv_doc.document_id})

        # Malformed CSV quoting raises csv.Error in parse_load_file (caught cleanly), while an incompletely read CSV in the same root_key as a completed DAT puts gamma in conflicted status -> list_sources withholds Bates and file_failure is surfaced.
        vol2_dat = dat_bytes(("BEGDOC", "ENDDOC", "NATIVEPATH"), [("SYN0001", "SYN0001", "NATIVES\\gamma.pdf")])
        incomplete_csv = b"BEGDOC,,NATIVEPATH\nSYN0099,x,NATIVES\\gamma.pdf\n"
        bad_csv = b'path,begin_bates,end_bates\n"Vol2/NATIVES/gamma.pdf,SYN0099,SYN0099\n'
        _upload_volume(review, {
            "Vol2/NATIVES/gamma.pdf": pdf_bytes(["Gamma page 1."]),
            "Vol2/DATA/records.dat": vol2_dat,
            "Vol2/DATA/incomplete.csv": incomplete_csv,
            "Vol2/DATA/malformed.csv": bad_csv,
        })
        from tests.test_discovery_review_index import service_for
        service = service_for(review.bench)
        for _ in range(200):
            assert review.client.get(review.base).status_code == 200
            state = service.repository.state(review.matter.matter_id)
            if state and state["phase"] == "complete" and not state["pending"] and state["errors"] >= 1:
                break
        gamma = review.document("Vol2/NATIVES/gamma.pdf")
        malformed_doc = review.document("Vol2/DATA/malformed.csv")
        bad_overview = runtime.invoke("inspect_production", {"load_file_document_id": malformed_doc.document_id})
        assert bad_overview["bates_gaps"]["computed"] is False
        assert "could not be computed" in bad_overview["bates_gaps"]["reason"]
        conflicted_list = {row["document_id"]: row for row in runtime.invoke("list_sources", {"limit": 50})["sources"]}
        assert conflicted_list[gamma.document_id]["bates"] == ""
        assert any(att["source_format"] == "csv" and att["status"] == "failed" and att["failure"] for att in bad_overview["load_file_attempts"])

        # Unauthorized actor fails closed.
        unauth = MatterToolRuntime(review.matter.matter_id, "unauth-actor", WorkbenchToolBackend(review.bench, "unauth-actor"))
        with pytest.raises(AssistantAccessError):
            unauth.invoke("inspect_production", {})

        # Non-boolean include_gaps fails closed.
        with pytest.raises(AssistantToolError):
            runtime.invoke("inspect_production", {"include_gaps": "yes"})
