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
        assert "post_buyback_outstanding(800000)" in page.text
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

