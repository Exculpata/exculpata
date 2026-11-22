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
