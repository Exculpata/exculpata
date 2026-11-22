"""End-to-end synthetic HTTP and authorization tests for Slice 3 workbench Case Assistant."""
from __future__ import annotations

import json
import re
from fastapi.testclient import TestClient

from case_intelligence.case_assistant import AssistantModelCall, AssistantTurn
from case_intelligence.workbench import create_workbench_app
from case_intelligence.workbench_assistant import WorkbenchToolBackend
from tests.test_milestone_a_workbench import _answer_and_wait, _create_matter


class _ScriptedWorkbenchAssistantClient:
    """Synthetic AssistantModelClient driving real WorkbenchService tools over HTTP."""

    available = True

    def __init__(self) -> None:
        self.turn, self.filing_doc_id, self.filing_name, self.filing_loc = 0, "", "", ""
        self.position_hits: list[dict[str, object]] = []

    def complete_turn(self, messages, *, tools):
        self.turn += 1
        last = messages[-1]
        if self.turn == 1:
            return AssistantTurn(tool_calls=(
                AssistantModelCall("w1", "list_sources", {"limit": 20}),
                AssistantModelCall("w2", "search_discovery", {"query": "buyback 800000", "mode": "hybrid", "limit": 5}),
                AssistantModelCall("w3", "search_discovery", {"query": '"stk-0"', "mode": "exact", "limit": 5}),
                AssistantModelCall("w4", "search_discovery", {"query": '"stk-1"', "mode": "exact", "limit": 5}),
                AssistantModelCall("w5", "get_case_knowledge", {"section": "notes", "limit": 10}),
            ))
        if self.turn == 2:
            tool_msgs = [json.loads(m["content"]) for m in messages if m.get("role") == "tool"]
            filing_hit = next(h for m in tool_msgs for h in m.get("hits", ()) if "800000" in h["excerpt"])
            self.filing_doc_id, self.filing_name, self.filing_loc = filing_hit["document_id"], filing_hit["source_name"], filing_hit["location"]
            for acc in ("stk-0", "stk-1"):
                self.position_hits.append(next(h for m in tool_msgs for h in m.get("hits", ()) if f"account={acc}" in h["excerpt"]))
            return AssistantTurn(tool_calls=(AssistantModelCall("w6", "read_source", {"document_id": self.filing_doc_id, "start_page": 1, "end_page": 1}),))
        if self.turn == 3:
            inputs = [{"label": f"stk-{i}", "value": re.search(r"shares=(\d+)", h["excerpt"]).group(1),
                       "source_refs": [{k: h[k] for k in ("document_id", "source_name", "location", "source_version_id")}]}
                      for i, h in enumerate(self.position_hits)]
            return AssistantTurn(tool_calls=(AssistantModelCall("w7", "calculate", {"operation": "sum", "inputs": inputs, "unit": "shares"}),))
        if self.turn == 4:
            combined = json.loads(last["content"])["result"]
            refs = [{k: h[k] for k in ("document_id", "source_name", "location", "source_version_id")} for h in self.position_hits]
            return AssistantTurn(tool_calls=(AssistantModelCall("w8", "calculate", {"operation": "percent", "inputs": [
                {"label": "combined_shares", "value": combined, "source_refs": refs},
                {"label": "post_buyback_outstanding", "value": "800000",
                 "source_refs": [{"document_id": self.filing_doc_id, "source_name": self.filing_name, "location": self.filing_loc}]},
            ]}),))
        pct = json.loads(last["content"])["result"]
        cites = [{k: h[k] for k in ("document_id", "source_name", "location", "source_version_id")} for h in self.position_hits]
        cites.append({"document_id": self.filing_doc_id, "source_name": self.filing_name, "location": self.filing_loc})
        return AssistantTurn(content=json.dumps({
            "answerable": True,
            "summary": f"Yes. Combined controlled holdings reached {pct}% of outstanding shares.",
            "findings": [{
                "text": f"Combined holdings across stk-0 and stk-1 totaled {pct}% of the 800000 post-buyback shares.",
                "citations": cites, "calculation_ids": ["calc-1", "calc-2"],
            }],
            "gaps": [],
            "competing_accounts": ["Witness A claimed no instructions were sent while emails show order schedules."],
        }))


def test_workbench_case_assistant_end_to_end_http_and_trace_rendering(tmp_path, monkeypatch):
    monkeypatch.setenv("CASE_INTELLIGENCE_CASE_ASSISTANT", "1")
    assistant_client = _ScriptedWorkbenchAssistantClient()
    app = create_workbench_app(tmp_path / "runtime", generator=assistant_client, auth_mode="test")
    with TestClient(app) as client:
        slug = _create_matter(client, "Synthetic Stock Matter")
        upload = client.post(f"/matters/{slug}/uploads", files=[
            ("files", ("buyback-filing.txt", b"Orbel Spindle Works buyback effective 2024-09-01 outstanding shares 800000.\n", "text/plain")),
            ("files", ("stmt-stk0.txt", b"Position date=2024-09-30 account=stk-0 shares=50000.\n", "text/plain")),
            ("files", ("stmt-stk1.txt", b"Position date=2024-09-30 account=stk-1 shares=40000.\n", "text/plain")),
            ("files", ("damaged-scan.pdf", b"%PDF-1.4\n%%corrupted-stream-body\n", "application/pdf")),
        ])
        assert upload.status_code == 200
        workspace = client.get(f"/matters/{slug}")
        conv_match = re.search(r'name="conversation" value="([^"]+)"', workspace.text)
        assert conv_match
        conv_id = conv_match.group(1)

        _, page = _answer_and_wait(client, slug, conv_id, "Did controlled accounts exceed 10% after the buyback?")
        assert page.status_code == 200
        assert "11.25%" in page.text
        assert "percent(90000, 800000) = 11.25" in page.text
        assert "damaged-scan.pdf" in page.text
        assert "Witness A claimed no instructions" in page.text
        assert "Work trace" in page.text

        dock = client.get(f"/matters/{slug}/assistant", params={"conversation": conv_id})
        assert dock.status_code == 200
        assert "11.25%" in dock.text and "Work trace" in dock.text

        backend = WorkbenchToolBackend(app.state.workbench, "unauthorized-actor")
        matter = app.state.workbench.workspace.get_matter(slug, "development-taylor-morgan")
        assert backend.authorize(matter.matter_id, "unauthorized-actor") is False


class _UnsupportedSummaryAssistantClient:
    available = True

    def __init__(self) -> None:
        self.turn = 0
        self.seen_notes: list[object] = []

    def complete_turn(self, messages, *, tools):
        self.turn += 1
        if self.turn == 1:
            return AssistantTurn(tool_calls=(
                AssistantModelCall("u1", "search_discovery", {"query": "bicycle", "mode": "exact", "limit": 5}),
                AssistantModelCall("u2", "get_case_knowledge", {"section": "notes", "limit": 10}),
            ))
        tool_msgs = [json.loads(m["content"]) for m in messages if m.get("role") == "tool"]
        hit = next(h for m in tool_msgs for h in m.get("hits", ()))
        self.seen_notes = next((m.get("notes", []) for m in tool_msgs if "notes" in m), [])
        cite = {k: hit[k] for k in ("document_id", "source_name", "location", "source_version_id")}
        return AssistantTurn(content=json.dumps({
            "answerable": True,
            "summary": "Unsupported 88.88% ownership summary.",
            "findings": [
                {"text": "The memo references a red bicycle.", "citations": [cite], "calculation_ids": []},
                {"text": "Unsupported 88.88% ownership claim.", "citations": [cite], "calculation_ids": []},
            ],
            "gaps": [],
            "competing_accounts": [],
        }))


def _minimal_partial_pdf() -> bytes:
    """Build a valid 3-page PDF where page 2 is blank and pages 1 and 3 carry text."""
    import io
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

    writer = PdfWriter()
    font = DictionaryObject({
        NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/Type1"),
        NameObject("/BaseFont"): NameObject("/Helvetica"),
    })
    font_ref = writer._add_object(font)
    for text in ("Physical page one text", "", "Physical page three text"):
        page = writer.add_blank_page(width=612, height=792)
        if text:
            page[NameObject("/Resources")] = DictionaryObject({
                NameObject("/Font"): DictionaryObject({NameObject("/F1"): font_ref}),
            })
            stream = DecodedStreamObject()
            stream.set_data(f"BT /F1 12 Tf 36 750 Td ({text}) Tj ET".encode("ascii"))
            page[NameObject("/Contents")] = writer._add_object(stream)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def test_pr587_regressions_state_scoped_exact_summary_knowledge_notes_and_partial_pdf(tmp_path, monkeypatch):
    monkeypatch.setenv("CASE_INTELLIGENCE_CASE_ASSISTANT", "1")
    assistant_client = _UnsupportedSummaryAssistantClient()
    app = create_workbench_app(tmp_path / "runtime", generator=assistant_client, auth_mode="test")
    with TestClient(app) as client:
        slug = _create_matter(client, "Synthetic Regression Matter")
        all_files = [("files", (f"doc-{i:02d}.txt", f"commonmarker red bicycle note {i}.\n".encode(), "text/plain")) for i in range(1, 27)]
        all_files.append(("files", ("partial-pages.pdf", _minimal_partial_pdf(), "application/pdf")))
        for idx in range(0, len(all_files), 9):
            assert client.post(f"/matters/{slug}/uploads", files=all_files[idx:idx + 9]).status_code == 200

        bench = app.state.workbench
        actor = "development-taylor-morgan"
        matter = bench.workspace.get_matter(slug, actor)
        backend = WorkbenchToolBackend(bench, actor)

        # Save a matter note, entity, and assertion via supported workbench services.
        bench.workspace.create_notebook_item(
            matter.matter_id, actor, item_type="note", title="Private working note",
            body="Do not expose when notes disabled.", status="confirmed",
        )
        exact_hit = backend.search_discovery(matter.matter_id, '"bicycle"', mode="exact", limit=1).hits[0]
        support_token = backend.citations_by_key[(exact_hit.document_id, exact_hit.location)].support_token
        entity = bench.entity_service(matter).create(
            matter.matter_id, actor, display_name="Jordan Ellis", support=support_token,
        )
        bench.assertion_service(matter).create(
            matter.matter_id, actor, support=support_token, attributed_to="Synthetic Report",
            roles=[{"entity_id": entity["entity_id"], "expected_revision": 1, "role": "subject"}],
            title="Phone call timing", statement="Call placed at 09:15 before market open.",
            raw_date="2024-09-10",
        )

        # 1. Ready sources are classified as ready, not failed.
        ready_rows, ready_total = backend.list_sources(matter.matter_id, status="ready", limit=50)
        failed_rows, failed_total = backend.list_sources(matter.matter_id, status="failed", limit=50)
        assert ready_total == 27 and failed_total == 0 and failed_rows == []

        # 2. Scoped exact search finds the 26th matching document beyond the first 25.
        last_doc = next(r for r in ready_rows if r.source_name == "doc-26.txt")
        scoped_resp = backend.search_discovery(
            matter.matter_id, '"commonmarker"', mode="exact", limit=5,
            document_ids=frozenset({last_doc.document_id}),
        )
        assert len(scoped_resp.hits) == 1 and scoped_resp.hits[0].document_id == last_doc.document_id

        # 3. Knowledge tool maps actual service fields (display_name, statement, raw_date).
        knowledge = backend.get_case_knowledge(matter.matter_id, section="all", limit=10)
        assert knowledge["entities"][0]["name"] == "Jordan Ellis"
        assert "09:15" in str(knowledge["assertions"][0]["summary"])
        assert knowledge["assertions"][0]["date"] == "2024-09-10"

        # 4. Partial PDF preserves physical page numbers (page 2 blank -> empty; page 3 -> Page 3).
        pdf_doc = next(r for r in ready_rows if r.source_name == "partial-pages.pdf")
        p2 = backend.read_source(matter.matter_id, pdf_doc.document_id, start_page=2, end_page=2)
        assert p2 is not None and p2.total_pages == 3 and p2.pages == ()
        p3 = backend.read_source(matter.matter_id, pdf_doc.document_id, start_page=3, end_page=3)
        assert p3 is not None and p3.total_pages == 3 and len(p3.pages) == 1
        assert p3.pages[0].page_number == 3 and "Physical page three text" in p3.pages[0].text

        # 5 & 6. HTTP run with default "Do not use case notes" hides unselected notes
        #        AND keeps unverified summary out of rendered/persisted answer text.
        workspace = client.get(f"/matters/{slug}")
        conv_id = re.search(r'name="conversation" value="([^"]+)"', workspace.text).group(1)
        _, page = _answer_and_wait(client, slug, conv_id, "What does the memo say about the bicycle?")
        assert page.status_code == 200
        assert "red bicycle" in page.text
        assert "88.88%" not in page.text
        assert assistant_client.seen_notes == []

        # 7. Hybrid search discloses its top-20 ranked bound (has_more=True) and directs offset>=20 to mode='exact'.
        import pytest
        from case_intelligence.case_assistant_tools import AssistantToolError
        hyb_p1 = backend.search_discovery(matter.matter_id, "commonmarker bicycle", mode="hybrid", limit=20, offset=0)
        assert len(hyb_p1.hits) == 20 and hyb_p1.has_more is True
        with pytest.raises(AssistantToolError):
            backend.search_discovery(matter.matter_id, "commonmarker bicycle", mode="hybrid", limit=20, offset=20)

        # 8. ExactSearchUnavailable raises AssistantToolError rather than returning fake zero matches.
        from case_intelligence.exact_search_results import ExactScanPolicy, ReferenceExactSearchBackend
        orig_backend = bench.exact_search_backend
        bench.exact_search_backend = ReferenceExactSearchBackend(ExactScanPolicy(max_documents=1))
        try:
            with pytest.raises(AssistantToolError):
                backend.search_discovery(matter.matter_id, '"commonmarker"', mode="exact", limit=5)
        finally:
            bench.exact_search_backend = orig_backend


def _four_page_matching_pdf() -> bytes:
    import io
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

    writer = PdfWriter()
    font = DictionaryObject({
        NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/Type1"),
        NameObject("/BaseFont"): NameObject("/Helvetica"),
    })
    font_ref = writer._add_object(font)
    for n in range(1, 5):
        page = writer.add_blank_page(width=612, height=792)
        page[NameObject("/Resources")] = DictionaryObject({
            NameObject("/Font"): DictionaryObject({NameObject("/F1"): font_ref}),
        })
        stream = DecodedStreamObject()
        stream.set_data(f"BT /F1 12 Tf 36 750 Td (quadmatch page {n} fact) Tj ET".encode("ascii"))
        page[NameObject("/Contents")] = writer._add_object(stream)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def test_pr587_round2_four_page_exact_and_large_unit_compact_citations(tmp_path, monkeypatch):
    monkeypatch.setenv("CASE_INTELLIGENCE_CASE_ASSISTANT", "1")
    assistant_client = _UnsupportedSummaryAssistantClient()
    app = create_workbench_app(tmp_path / "runtime", generator=assistant_client, auth_mode="test")
    with TestClient(app) as client:
        slug = _create_matter(client, "Synthetic Compact Citation Matter")
        huge_txt = ("bicycle " + ("a" * 58_500) + "\n" + "bicycle " + ("b" * 58_500) + "\n").encode("utf-8")
        assert client.post(f"/matters/{slug}/uploads", files=[
            ("files", ("quad.pdf", _four_page_matching_pdf(), "application/pdf")),
            ("files", ("huge.txt", huge_txt, "text/plain")),
        ]).status_code == 200

        bench = app.state.workbench
        actor = "development-taylor-morgan"
        matter = bench.workspace.get_matter(slug, actor)
        backend = WorkbenchToolBackend(bench, actor)

        # All 4 matching PDF pages are returned beyond the 3-unit exact-search preview cap.
        quad = backend.search_discovery(matter.matter_id, '"quadmatch"', mode="exact", limit=10)
        assert quad.total_matches == 4 and len(quad.hits) == 4 and quad.has_more is False

        # Compact saved-answer citations succeed on >100KB source units without hitting finish_answer_job payload limit.
        workspace = client.get(f"/matters/{slug}")
        conv_id = re.search(r'name="conversation" value="([^"]+)"', workspace.text).group(1)
        _, page = _answer_and_wait(client, slug, conv_id, "Check bicycle mention.")
        assert page.status_code == 200 and "red bicycle" in page.text


def _five_page_four_match_pdf() -> bytes:
    """Build a 5-page PDF where 4 pages match 'quadmatch' and page 5 is unrelated."""
    from io import BytesIO
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

    writer = PdfWriter()
    lines = [
        "quadmatch entry one",
        "quadmatch entry two",
        "quadmatch entry three",
        "quadmatch entry four",
        "completely unrelated fifth page",
    ]
    for text in lines:
        page = writer.add_blank_page(width=300, height=144)
        font = DictionaryObject({
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        })
        page[NameObject("/Resources")] = DictionaryObject({
            NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)}),
        })
        stream = DecodedStreamObject()
        stream.set_data(f"BT /F1 12 Tf 24 72 Td ({text}) Tj ET".encode("ascii"))
        page[NameObject("/Contents")] = writer._add_object(stream)
    buf = BytesIO()
    writer.write(buf)
    return buf.getvalue()


def test_pr587_round3_exact_filter_exports_reports_and_knowledge_pagination(tmp_path, monkeypatch):
    from case_intelligence.case_assistant_tools import MatterToolRuntime
    from case_intelligence.report_materials import snapshot_report_materials
    from case_intelligence.work_product_exports import _answer_blocks
    from case_intelligence.workbench_assistant import WorkbenchToolBackend

    monkeypatch.setenv("CASE_INTELLIGENCE_CASE_ASSISTANT", "1")
    app = create_workbench_app(tmp_path / "runtime", generator=_UnsupportedSummaryAssistantClient(), auth_mode="test")
    with TestClient(app) as client:
        slug = _create_matter(client, "Synthetic Round 3 Matter")
        assert client.post(f"/matters/{slug}/uploads", files=[
            ("files", ("five.pdf", _five_page_four_match_pdf(), "application/pdf")),
            ("files", ("note.txt", b"Ledger entry for Entity One through Entity Nine.", "text/plain")),
        ]).status_code == 200

        bench = app.state.workbench
        actor = "development-taylor-morgan"
        matter = bench.workspace.get_matter(slug, actor)
        backend = WorkbenchToolBackend(bench, actor)
        runtime = MatterToolRuntime(matter.matter_id, actor, backend)

        # 1. Exact search expansion consumes positional matches and excludes the unrelated 5th page.
        exact = backend.search_discovery(matter.matter_id, '"quadmatch"', mode="exact", limit=10)
        assert exact.total_matches == 4 and len(exact.hits) == 4 and exact.has_more is False
        assert not any("unrelated fifth page" in h.excerpt.casefold() for h in exact.hits)

        # 2. Saved-knowledge pagination traverses service pages beyond 8 entities / 6 assertions.
        cites = bench.search(matter, "Ledger entry", limit=5)
        support_token = cites[0].support_token
        ent_svc = bench.entity_service(matter)
        asn_svc = bench.assertion_service(matter)
        entity_ids = []
        for idx in range(1, 10):
            ent = ent_svc.create(
                matter.matter_id, actor, display_name=f"Entity {idx:02d}",
                entity_type="organization", support=support_token,
            )
            entity_ids.append(ent["entity_id"])
        for idx in range(1, 8):
            asn_svc.create(
                matter.matter_id, actor, title=f"Assertion {idx:02d}",
                statement=f"Statement {idx:02d} in synthetic ledger.",
                raw_date=f"2024-09-{idx:02d}",
                attributed_to="Synthetic ledger",
                roles=[{"entity_id": entity_ids[0], "expected_revision": 1, "role": "subject"}],
                support=support_token,
            )
        full_k = runtime.invoke("get_case_knowledge", {"section": "all", "limit": 50})
        assert len(full_k["entities"]) == 9 and full_k["entities_total"] == 9 and full_k["entities_omitted"] == 0
        assert len(full_k["assertions"]) == 7 and full_k["assertions_total"] == 7 and full_k["assertions_omitted"] == 0
        assert full_k["has_more"] is False and full_k["next_offset"] is None

        p1 = runtime.invoke("get_case_knowledge", {"section": "entities", "limit": 5, "offset": 0})
        assert len(p1["entities"]) == 5 and p1["entities_omitted"] == 4 and p1["has_more"] is True and p1["next_offset"] == 5
        p2 = runtime.invoke("get_case_knowledge", {"section": "entities", "limit": 5, "offset": 5})
        assert len(p2["entities"]) == 4 and p2["entities_omitted"] == 0 and p2["has_more"] is False

        # 3. Export blocks, report_materials, and add_answer_to_report preserve gaps, competing accounts, and receipts.
        conv = bench.workspace.create_conversation(matter.matter_id, "Qualifications Check", actor_id=actor)
        bench.workspace.append_message(matter.matter_id, conv.conversation_id, "user", "Summarize holdings.", {})
        msg = bench.workspace.append_message(
            matter.matter_id, conv.conversation_id, "assistant", "Holdings reached 12.5%.",
            {
                "kind": "generated",
                "answerable": True,
                "introduction": "Verified findings from inspected matter sources:",
                "claims": [{"text": "Holdings reached 12.5%.", "citations": [bench._saved_answer_citation_payload(cites[0])]}],
                "limitation": None,
                "case_assistant": {
                    "gaps": ["October statement is missing from production."],
                    "competing_accounts": ["Witness A denies control while email directs trades."],
                    "receipts": [{"receipt_id": "calc-1", "expression": "percent(125, 1000)", "result": "12.50", "unit": "%"}],
                },
            },
        )
        export_text = "\n".join(b.text for b in _answer_blocks(msg))
        assert "October statement is missing from production." in export_text
        assert "Witness A denies control while email directs trades." in export_text
        assert "calc-1" in export_text and "12.50" in export_text

        materials = snapshot_report_materials(bench, matter, actor, (f"conversation:{conv.conversation_id}",))
        mat_texts = "\n".join(m.text for m in materials)
        assert "October statement is missing from production." in mat_texts
        assert "Witness A denies control while email directs trades." in mat_texts
        assert "calc-1" in mat_texts and "12.50" in mat_texts

        report = bench.workspace.create_report(matter.matter_id, actor, "Defense Report")
        section = bench.add_answer_to_report(
            matter, actor, report.report_id, conv.conversation_id, msg.message_id, expected_status=report.status,
        )
        assert "October statement is missing from production." in section.body
        assert "Witness A denies control while email directs trades." in section.body
        assert "calc-1" in section.body and "12.50" in section.body


def test_pr587_round4_identity_bound_citations_and_label_free_receipts(tmp_path):
    from types import SimpleNamespace
    from case_intelligence.case_assistant import AssistantFinding, AssistantInvestigationResult
    from case_intelligence.case_assistant_tools import CalculationInput, CalculationReceipt, SourceRef
    from case_intelligence.workbench_assistant import (
        WorkbenchToolBackend, _result_to_answer, format_assistant_qualifications,
    )

    app = create_workbench_app(tmp_path / "runtime", auth_mode="test")
    with TestClient(app) as client:
        slug = _create_matter(client, "Synthetic Round 4 Identity Matter")
        assert client.post(f"/matters/{slug}/uploads", files=[
            ("files", ("ledger.txt", b"Balance 10 on page one.", "text/plain")),
        ]).status_code == 200
        bench = app.state.workbench
        actor = "development-taylor-morgan"
        matter = bench.workspace.get_matter(slug, actor)
        backend = WorkbenchToolBackend(bench, actor)
        orig_wc = bench.search(matter, "Balance 10", limit=1)[0]
        edited_wc = SimpleNamespace(
            matter_id=orig_wc.matter_id,
            document_id=orig_wc.document_id,
            source_version_id=orig_wc.source_version_id,
            source_name=orig_wc.source_name,
            location=orig_wc.location,
            unit_number=orig_wc.unit_number,
            chunk_id=orig_wc.chunk_id,
            excerpt="Balance 15 on page one.",
            excerpt_digest="changed-digest-0002",
            evidence_kind=orig_wc.evidence_kind,
            support_token="changed-token-0002",
        )
        backend._remember_citation(orig_wc)
        backend._remember_citation(edited_wc)

        # 1a. Ref carrying the original digest resolves strictly to the original citation, never the edited one.
        orig_ref = SourceRef(orig_wc.document_id, orig_wc.source_name, orig_wc.location,
                             orig_wc.source_version_id, orig_wc.excerpt_digest)
        resolved = backend.citation_for_ref(orig_ref)
        assert resolved is orig_wc and resolved.excerpt_digest == orig_wc.excerpt_digest

        # 1b. Digestless ref fails closed when multiple digests exist for the same (doc, location, version).
        ambiguous_ref = SourceRef(orig_wc.document_id, orig_wc.source_name, orig_wc.location,
                                  orig_wc.source_version_id, "")
        assert backend.citation_for_ref(ambiguous_ref) is None
        ambiguous_result = AssistantInvestigationResult(
            answerable=True, summary="Balance 10.",
            findings=(AssistantFinding("Balance 10.", (ambiguous_ref,), ()),),
            omitted_findings=0, gaps=(), competing_accounts=(),
            citations=(ambiguous_ref,), receipts={}, trace=(), turns_used=1, elapsed_ms=1,
        )
        ans_amb = _result_to_answer(bench, ambiguous_result, backend, "fp")
        assert ans_amb.payload["kind"] == "not-supported"
        assert ans_amb.payload["claims"] == []
        assert ans_amb.payload["case_assistant"]["omitted_findings"] == 1

        # 2. Unverified model labels and unverified unit text never appear in verified calculation display.
        receipt = CalculationReceipt(
            receipt_id="calc-1",
            operation="sum",
            expression="fabricated 87.65(10) = 10",
            result="10",
            unit="USD; fabricated ownership 87.65%",
            inputs=(CalculationInput("fabricated 87.65", "10", (orig_ref,)),),
        )
        unreferenced_receipt = CalculationReceipt(
            receipt_id="calc-2",
            operation="percent",
            expression="10 / 100 = 10",
            result="10",
            unit="%",
            inputs=(CalculationInput("part", "10", (orig_ref,)),),
        )
        missing_premise_receipt = CalculationReceipt(
            receipt_id="calc-3",
            operation="sum",
            expression="5 = 5",
            result="5",
            unit="",
            inputs=(CalculationInput("ghost", "5", (ambiguous_ref,)),),
        )
        calc_result = AssistantInvestigationResult(
            answerable=True, summary="Total is 10.",
            findings=(AssistantFinding("Balance is 10.", (orig_ref,), ("calc-1",)),),
            omitted_findings=0, gaps=(), competing_accounts=(),
            citations=(orig_ref,),
            receipts={"calc-1": receipt, "calc-2": unreferenced_receipt, "calc-3": missing_premise_receipt},
            trace=(), turns_used=1, elapsed_ms=1,
        )
        ans_calc = _result_to_answer(bench, calc_result, backend, "fp")
        saved_receipts = {r["receipt_id"]: r for r in ans_calc.payload["case_assistant"]["receipts"]}
        assert set(saved_receipts) == {"calc-1", "calc-2"}
        assert saved_receipts["calc-1"]["expression"] == "sum(10) = 10"
        assert saved_receipts["calc-1"]["unit"] == ""
        assert saved_receipts["calc-2"]["unit"] == "%"
        assert orig_wc.support_token in {c.support_token for c in ans_calc.citations}
        qual = format_assistant_qualifications(ans_calc.payload)
        assert "sum(10) = 10" in qual and "fabricated" not in qual and "87.65" not in qual

        # 3. Pure NOT exact search preserves matching documents with empty positive passages.
        assert client.post(f"/matters/{slug}/uploads", files=[
            ("files", ("blue.txt", b"Blue ledger with no crimson mention.", "text/plain")),
        ]).status_code == 200
        not_red = backend.search_discovery(matter.matter_id, "NOT red", mode="exact", limit=10)
        assert not_red.total_matches == 2 and len(not_red.hits) == 2

        # 4. Unverified factual competing_accounts (unsupported numbers or zero retained citations) are omitted.
        bad_competing = AssistantInvestigationResult(
            answerable=True, summary="Balance is 10.",
            findings=(AssistantFinding("Balance is 10.", (orig_ref,), ()),),
            omitted_findings=0, gaps=(),
            competing_accounts=(
                "A witness reports unsupported 88.88% ownership.",
                "Another note reflects a balance of 10 on page one.",
            ),
            citations=(orig_ref,), receipts={}, trace=(), turns_used=1, elapsed_ms=1,
        )
        ans_comp = _result_to_answer(bench, bad_competing, backend, "fp")
        kept = ans_comp.payload["case_assistant"]["competing_accounts"]
        assert kept == ["Another note reflects a balance of 10 on page one."]
        assert "88.88" not in format_assistant_qualifications(ans_comp.payload)

        no_cite_competing = AssistantInvestigationResult(
            answerable=False, summary="Abstained.", findings=(),
            omitted_findings=1,
            gaps=(
                "The 88.88% ownership transfer is missing its supporting statement.",
                "The balance of 15 is missing its supporting statement.",
                "1 unavailable source(s) could not be searched: damaged-scan.pdf",
                "October statement is missing from production.",
            ),
            competing_accounts=("Unsupported witness claim without source support.",),
            citations=(), receipts={}, trace=(), turns_used=1, elapsed_ms=1,
        )
        ans_none = _result_to_answer(bench, no_cite_competing, backend, "fp")
        assert ans_none.payload["case_assistant"]["competing_accounts"] == []
        kept_gaps = ans_none.payload["case_assistant"]["gaps"]
        assert "1 unavailable source(s) could not be searched: damaged-scan.pdf" in kept_gaps
        assert "October statement is missing from production." in kept_gaps
        assert not any("88.88" in g for g in kept_gaps)
        assert not any("balance of 15" in g for g in kept_gaps)







