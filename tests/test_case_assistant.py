"""Synthetic proving and fail-closed tests for the Case Assistant tool loop."""
from __future__ import annotations

import io
import json
import re
import pytest

from case_intelligence.case_assistant import (
    AssistantModelCall, AssistantTurn, CaseAssistant,
    OllamaAssistantClient, OpenAIAssistantClient, validate_assistant_tool_payload,
)
from case_intelligence.case_assistant_tools import AssistantAccessError, MatterToolRuntime
from case_intelligence.generator_schema import ToolRequestRefused
from tests.test_case_assistant_tools import _stock_backend


class _ScriptedStockModel:
    """Deterministic model driving real multi-turn tools against the stock corpus."""

    def __init__(self, *, inject_hallucination: bool = False, fabricate_citation: bool = False) -> None:
        self.inject_hallucination, self.fabricate_citation = inject_hallucination, fabricate_citation
        self.turn, self.buyback_hit, self.buyback_shares, self.account_hits = 0, None, "", []

    def complete_turn(self, messages, *, tools):
        self.turn += 1
        last = messages[-1]
        if self.turn == 1:
            return AssistantTurn(tool_calls=(
                AssistantModelCall("c1", "list_sources", {"limit": 10}),
                AssistantModelCall("c2", "search_discovery", {"query": "buyback 2024-09-01 shares", "limit": 5}),
            ))
        if self.turn == 2:
            self.buyback_hit = next(h for h in json.loads(last["content"])["hits"] if "event=buyback" in h["excerpt"])
            calls = [AssistantModelCall("c3", "read_source", {"document_id": self.buyback_hit["document_id"], "start_page": 1, "end_page": 2}),
                     *(AssistantModelCall(f"c{4 + i}", "search_discovery", {"query": f"Position 2024-09-30 {acc}", "limit": 5})
                       for i, acc in enumerate(("stk-0", "stk-1", "stk-2", "stk-3")))]
            return AssistantTurn(tool_calls=tuple(calls))
        if self.turn == 3:
            tool_msgs = [json.loads(m["content"]) for m in messages if m.get("role") == "tool"]
            self.buyback_shares = re.search(r"shares:\s*(\d+)", next(m for m in tool_msgs if "pages" in m)["pages"][1]["text"]).group(1)
            inputs = []
            for acc in ("stk-0", "stk-1", "stk-2", "stk-3"):
                match = next(h for msg in tool_msgs for h in msg.get("hits", ()) if f"date=2024-09-30; account={acc}" in h["excerpt"])
                self.account_hits.append(match)
                inputs.append({"label": acc, "value": re.search(r"shares=(\d+)", match["excerpt"]).group(1),
                               "source_refs": [{k: match[k] for k in ("document_id", "source_name", "location", "source_version_id")}]})
            return AssistantTurn(tool_calls=(AssistantModelCall("c8", "calculate", {"operation": "sum", "inputs": inputs, "unit": "shares"}),))
        if self.turn == 4:
            refs = [{k: h[k] for k in ("document_id", "source_name", "location", "source_version_id")} for h in self.account_hits]
            return AssistantTurn(tool_calls=(AssistantModelCall("c9", "calculate", {"operation": "percent", "inputs": [
                {"label": "combined_controlled", "value": json.loads(last["content"])["result"], "source_refs": refs},
                {"label": "buyback_outstanding", "value": self.buyback_shares,
                 "source_refs": [{"document_id": self.buyback_hit["document_id"], "source_name": self.buyback_hit["source_name"], "location": "Page 2"}]},
            ]}),))
        pct = json.loads(last["content"])["result"]
        cites = [{k: h[k] for k in ("document_id", "source_name", "location", "source_version_id")} for h in self.account_hits]
        cites.append({"document_id": self.buyback_hit["document_id"], "source_name": self.buyback_hit["source_name"], "location": "Page 2"})
        findings = [{"text": f"Combined holdings across stk-0 through stk-3 reached {pct}% after the 2024-09-01 buyback.",
                     "citations": cites, "calculation_ids": ["calc-1", "calc-2"]}]
        if self.inject_hallucination:
            findings.append({"text": "An unverified filing showed 87.65% ownership.", "citations": cites[:1], "calculation_ids": []})
        if self.fabricate_citation:
            findings.append({"text": "Fabricated citation check.", "citations": [{"document_id": "fake-999", "source_name": "fake.pdf", "location": "Page 1"}], "calculation_ids": []})
        return AssistantTurn(content=json.dumps({"answerable": True, "summary": f"Yes. Combined controlled holdings reached {pct}%.",
                                                 "findings": findings, "gaps": [], "competing_accounts": []}))


class _FakeOpener:
    def __init__(self, responses: list[dict[str, object]]) -> None:
        self.responses, self.calls = list(responses), []
    def open(self, request, **kwargs):
        self.calls.append(json.loads(request.data))
        return io.BytesIO(json.dumps(self.responses.pop(0)).encode("utf-8"))


def test_case_assistant_solves_stock_ownership_with_verified_math_and_gaps():
    stages: list[tuple[str, str]] = []
    runtime = MatterToolRuntime("matter-stock", "defender-1", _stock_backend())
    result = CaseAssistant(_ScriptedStockModel()).investigate(
        "Did the subject or controlled accounts ever own more than 10% of the stock?",
        runtime, stage_callback=lambda key, msg: stages.append((key, msg)),
    )
    assert result.answerable is True and result.omitted_findings == 0
    assert len(result.findings) == 1 and "calc-2" in result.findings[0].calculation_ids
    assert float(result.receipts["calc-2"].result) > 10.0
    assert any("damaged-scan.pdf" in gap or "1 unavailable" in gap for gap in result.gaps)
    assert any(key == "verifying" for key, _ in stages)


def test_verifier_omits_hallucinated_numbers_and_fabricated_citations():
    runtime = MatterToolRuntime("matter-stock", "defender-1", _stock_backend())
    result = CaseAssistant(_ScriptedStockModel(inject_hallucination=True, fabricate_citation=True)).investigate("Ownership check", runtime)
    assert result.answerable is True and len(result.findings) == 1 and result.omitted_findings == 2


def test_revoked_access_and_non_allowlisted_tools_fail_closed():
    backend = _stock_backend()
    with pytest.raises(AssistantAccessError):
        CaseAssistant(_ScriptedStockModel()).investigate("Ownership check", MatterToolRuntime("matter-stock", "intruder-1", backend))
    assert backend.calls == []
    with pytest.raises(ToolRequestRefused):
        validate_assistant_tool_payload({"tools": [{"type": "function", "function": {"name": "run_shell"}}], "messages": []})
    with pytest.raises(ToolRequestRefused):
        validate_assistant_tool_payload({"functions": [], "messages": []})


def test_openai_and_ollama_clients_parse_tool_turns_and_enforce_allowlist():
    openai_opener = _FakeOpener([{"choices": [{"message": {"content": "", "tool_calls": [
        {"id": "tc1", "type": "function", "function": {"name": "list_sources", "arguments": "{\"limit\": 5}"}}]}}]}])
    turn = OpenAIAssistantClient("http://127.0.0.1:8790", "synthetic-model", opener=openai_opener).complete_turn(
        [{"role": "user", "content": "check"}], tools=[{"type": "function", "function": {"name": "list_sources"}}])
    assert len(turn.tool_calls) == 1 and turn.tool_calls[0].arguments == {"limit": 5}

    ollama_opener = _FakeOpener([{"message": {"content": "", "tool_calls": [{"function": {"name": "list_sources", "arguments": {"limit": 5}}}]}}])
    oturn = OllamaAssistantClient("http://127.0.0.1:8790", "synthetic-model", opener=ollama_opener).complete_turn(
        [{"role": "user", "content": "check"}], tools=[{"type": "function", "function": {"name": "list_sources"}}])
    assert len(oturn.tool_calls) == 1 and oturn.tool_calls[0].name == "list_sources"
