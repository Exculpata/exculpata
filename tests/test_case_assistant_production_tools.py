"""Synthetic proving and fail-closed tests for Case Assistant production and Bates tools."""
from __future__ import annotations

import json
from pathlib import Path
import time
import uuid

import pytest

from case_intelligence.case_assistant import AssistantModelCall, AssistantTurn, CaseAssistant, validate_assistant_tool_payload
from case_intelligence.case_assistant_tools import (
    ALLOWED_TOOLS, AssistantAccessError, AssistantToolError, MatterToolRuntime, openai_tool_schemas,
)
from case_intelligence.modules import ModuleDatabase
from case_intelligence.modules.discovery_review.identifiers import IdentifierRepository
from case_intelligence.workbench_assistant import WorkbenchToolBackend, _result_to_answer
from tests.test_discovery_review_dat_opt import ACTOR, complete_listing, dat_bytes, opened_review
from tests.test_discovery_review_index import service_for
from tests.test_intake_receipt_http import put, selection, upload
from tests.test_workflow_quality_handoff import pdf_bytes


def _upload_volume(review, files):
    media = {".pdf": "application/pdf", ".csv": "text/csv", ".dat": "text/plain", ".txt": "text/plain"}
    names = list(files)
    descriptors = [{"name": Path(n).name, "relative_path": n, "size": len(files[n]),
                    "media_type": media.get(Path(n).suffix.lower(), "text/plain")} for n in names]
    ordinals = list(range(len(names)))
    receipt = selection(review.client, review.matter.slug, descriptors, ordinals, key=uuid.uuid4().hex)
    session = upload(review.client, review.matter.slug, receipt, descriptors, ordinals)
    for name, item in zip(names, session["items"]):
        put(review.client, item, files[name], offset=0)
        assert review.client.post(item["finalize_url"]).status_code == 200
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        items = [review.bench.workspace.upload_item(review.matter.matter_id, ACTOR, session["upload_session_id"], item["upload_item_id"]) for item in session["items"]]
        if all((job := review.bench.workspace.ingest_job(review.matter.matter_id, item.document_id)) is not None and job.state in {"succeeded", "failed"} for item in items):
            break
        time.sleep(0.01)


def test_inspect_production_schema_and_allowlist():
    schemas = openai_tool_schemas()
    assert "inspect_production" in ALLOWED_TOOLS and "inspect_production" in {e["function"]["name"] for e in schemas}
    validate_assistant_tool_payload({"tools": schemas, "messages": [{"role": "user", "content": "Check production"}]})


def test_list_sources_bates_and_inspect_production_end_to_end(tmp_path, monkeypatch):

    monkeypatch.setenv("CASE_INTELLIGENCE_STORAGE_RESERVE_GIB", "0")
    monkeypatch.setenv("CASE_INTELLIGENCE_OCR_MODE", "disabled")
    dat = dat_bytes(("BEGDOC", "ENDDOC", "NATIVEPATH"), [
        ("PROD0001", "PROD0002", "NATIVES\\alpha.pdf"), ("PROD0004", "PROD0004", "NATIVES\\beta.pdf"),
    ])
    csv_load = b"path,begin_bates,end_bates\nVol1/NATIVES/alpha.pdf,PROD0001,PROD0002\nVol1/NATIVES/beta.pdf,PROD0004,PROD0004\n"
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
        alpha, extra, csv_doc = (review.document("Vol1/NATIVES/alpha.pdf"), review.document("Vol1/NATIVES/extra.txt"),
                                 review.document("Vol1/DATA/bates.csv"))
        IdentifierRepository(
            ModuleDatabase(review.bench.workspace.connection, review.bench.workspace._lock),
            review.bench.workspace.membership,
        ).assign(review.matter.matter_id, ACTOR, [(extra.document_id, "TEAM0009")])
        backend = WorkbenchToolBackend(review.bench, ACTOR)
        runtime = MatterToolRuntime(review.matter.matter_id, ACTOR, backend)

        by_id = {row["document_id"]: row for row in runtime.invoke("list_sources", {"limit": 50})["sources"]}
        assert by_id[alpha.document_id]["bates"] == "PROD0001\u2013PROD0002" and by_id[extra.document_id]["bates"] == "Team ID: TEAM0009"
        assert [row["document_id"] for row in runtime.invoke("list_sources", {"query": "PROD0001", "limit": 10})["sources"]] == [alpha.document_id]

        overview = runtime.invoke("inspect_production", {"load_file_document_id": csv_doc.document_id})
        assert overview["total_sources"] == 6 and overview["counts_by_media_type"]["application/pdf"] == 3 and overview["recorded_pdf_pages"] == 3
        assert any(item["source_name"].endswith("broken.pdf") for item in overview["unavailable_sources"])
        assert any(att["source_format"] == "dat" and att["status"] == "complete" for att in overview["load_file_attempts"])
        assert overview["bates_gaps"]["notice"] and any("PROD0003" in fact["text"] for fact in overview["bates_gaps"]["facts"])

        dat_doc = review.document("Vol1/DATA/records.dat")
        scoped = MatterToolRuntime(review.matter.matter_id, ACTOR, backend, allowed_document_ids=frozenset({alpha.document_id, dat_doc.document_id}))
        scoped_overview = scoped.invoke("inspect_production", {})
        assert scoped_overview["total_sources"] == 2 and scoped_overview["load_file_attempts"] == [] and scoped_overview["bates_gaps"]["computed"] is False
        with pytest.raises(AssistantAccessError):
            scoped.invoke("inspect_production", {"load_file_document_id": csv_doc.document_id})

        _upload_volume(review, {
            "Vol2/NATIVES/gamma.pdf": pdf_bytes(["Gamma page 1."]),
            "Vol2/DATA/records.dat": dat_bytes(("BEGDOC", "ENDDOC", "NATIVEPATH"), [("SYN0001", "SYN0001", "NATIVES\\gamma.pdf")]),
            "Vol2/DATA/incomplete.csv": b"BEGDOC,,NATIVEPATH\nSYN0099,x,NATIVES\\gamma.pdf\n",
            "Vol2/DATA/malformed.csv": b'path,begin_bates,end_bates\n"Vol2/NATIVES/gamma.pdf,SYN0099,SYN0099\n',
        })
        service = service_for(review.bench)
        for _ in range(200):
            assert review.client.get(review.base).status_code == 200
            state = service.repository.state(review.matter.matter_id)
            if state and state["phase"] == "complete" and not state["pending"] and state["errors"] >= 1:
                break
        gamma, malformed_doc = review.document("Vol2/NATIVES/gamma.pdf"), review.document("Vol2/DATA/malformed.csv")
        bad_overview = runtime.invoke("inspect_production", {"load_file_document_id": malformed_doc.document_id})
        assert bad_overview["bates_gaps"]["computed"] is False and "could not be computed" in bad_overview["bates_gaps"]["reason"]
        assert {row["document_id"]: row for row in runtime.invoke("list_sources", {"limit": 50})["sources"]}[gamma.document_id]["bates"] == ""
        assert any(att["source_format"] == "csv" and att["status"] == "failed" and att["failure"] for att in bad_overview["load_file_attempts"])

        class _ProdAssistantClient:
            def __init__(self, *, cite_receipt: bool = True): self.calls, self.cite_receipt = 0, cite_receipt
            def complete_turn(self, messages, *, tools):
                self.calls += 1
                if self.calls == 1:
                    return AssistantTurn(tool_calls=(AssistantModelCall("p1", "inspect_production", {"load_file_document_id": csv_doc.document_id}),))
                payload = json.loads(messages[-1]["content"])
                n_src, n_pages = payload["total_sources"], payload["recorded_pdf_pages"]
                fact_cite = payload["bates_gaps"]["facts"][0]["source_refs"][0]
                text = (f"The production has {n_src} sources, {n_pages} recorded PDF pages, and Missing Bates PROD0003."
                        if self.cite_receipt else f"The witness held {n_src} shares.")
                return AssistantTurn(content=json.dumps({
                    "answerable": True, "summary": text,
                    "findings": [{"text": text, "citations": [fact_cite],
                                  "calculation_ids": [payload["calculation"]["receipt_id"]] if self.cite_receipt else []}],
                    "gaps": [], "competing_accounts": [],
                }))
        inv = CaseAssistant(_ProdAssistantClient()).investigate("Counts and Bates gaps?", MatterToolRuntime(review.matter.matter_id, ACTOR, backend))
        assert inv.answerable is True and len(inv.findings) == 1 and inv.receipts
        ungrounded = CaseAssistant(_ProdAssistantClient(cite_receipt=False)).investigate("Shares?", MatterToolRuntime(review.matter.matter_id, ACTOR, backend))
        assert ungrounded.answerable is False and not ungrounded.findings
        converted = _result_to_answer(review.bench, inv, backend, "")
        assert converted.payload["answerable"] is True and len(converted.payload["claims"]) == 1 and converted.payload["case_assistant"]["receipts"]
        assert any(c["document_id"] == csv_doc.document_id for c in converted.payload["claims"][0]["citations"])
        assert overview["load_file_attempts_truncated"] is False and any(
            r["document_id"] == csv_doc.document_id for r in overview["bates_gaps"]["facts"][0]["source_refs"]
        )

        seen_lookups: list[int] = []
        orig_map = backend._document_bates_map
        monkeypatch.setattr(backend, "_document_bates_map", lambda mid, doc_ids: (seen_lookups.append(len(doc_ids)), orig_map(mid, doc_ids))[1])
        assert len(runtime.invoke("list_sources", {"limit": 1})["sources"]) == 1 and seen_lookups[0] == 1 and max(seen_lookups) <= 2
        monkeypatch.setattr(backend, "_document_bates_map", orig_map)

        unauth = MatterToolRuntime(review.matter.matter_id, "unauth-actor", WorkbenchToolBackend(review.bench, "unauth-actor"))
        with pytest.raises(AssistantAccessError):
            unauth.invoke("inspect_production", {})
        with pytest.raises(AssistantToolError):
            runtime.invoke("inspect_production", {"include_gaps": "yes"})
