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


class _FakeCalcModel:
    def __init__(self) -> None:
        self.step = 0
    def complete_turn(self, messages, *, tools):
        self.step += 1
        if self.step == 1:
            return AssistantTurn(tool_calls=(AssistantModelCall("f1", "calculate", {
                "operation": "sum", "inputs": [{"label": "x", "value": "87.65", "source_refs": [
                    {"document_id": "fake-999", "source_name": "fake.pdf", "location": "Page 1"}]}]}),))
        return AssistantTurn(content=json.dumps({
            "answerable": True, "summary": "Ownership was 87.65%.",
            "findings": [{"text": "Ownership was 87.65%.", "citations": [
                {"document_id": "fake-999", "source_name": "fake.pdf", "location": "Page 1"}], "calculation_ids": ["calc-1"]}],
            "gaps": [], "competing_accounts": [],
        }))


class _BadSummaryModel(_ScriptedStockModel):
    def complete_turn(self, messages, *, tools):
        turn = super().complete_turn(messages, tools=tools)
        if not turn.tool_calls:
            parsed = json.loads(turn.content)
            parsed["summary"] = "Unsupported summary claiming 99.99%."
            return AssistantTurn(content=json.dumps(parsed))
        return turn


class _LedgerBackend:
    def authorize(self, m, a): return True
    def list_sources(self, m, **kw):
        from case_intelligence.case_assistant_tools import SourceSummary
        return ([SourceSummary("l1", "ledger.txt", "txt", "ready", 1)], 1)
    def search_discovery(self, m, q, **kw): return ()
    def read_source(self, m, d, **kw):
        from case_intelligence.case_assistant_tools import SourcePage, SourceReadResult
        return SourceReadResult("l1", "ledger.txt", 1, (SourcePage(1, "Page 1", "Opening balance was 10 and debit was 15.", "v1"),))
    def get_case_knowledge(self, m, **kw): return {}


class _SignModel:
    def __init__(self) -> None:
        self.step = 0
    def complete_turn(self, messages, *, tools):
        self.step += 1
        if self.step == 1:
            return AssistantTurn(tool_calls=(AssistantModelCall("s1", "read_source", {"document_id": "l1", "start_page": 1}),))
        if self.step == 2:
            ref = {"document_id": "l1", "source_name": "ledger.txt", "location": "Page 1"}
            return AssistantTurn(tool_calls=(AssistantModelCall("s2", "calculate", {
                "operation": "subtract", "inputs": [
                    {"label": "open", "value": "10", "source_refs": [ref]},
                    {"label": "debit", "value": "15", "source_refs": [ref]},
                ]}),))
        ref = {"document_id": "l1", "source_name": "ledger.txt", "location": "Page 1"}
        return AssistantTurn(content=json.dumps({
            "answerable": True, "summary": "Net balance is -5.",
            "findings": [
                {"text": "Balance is -5.", "citations": [ref], "calculation_ids": ["calc-1"]},
                {"text": "Opening balance was -10.", "citations": [ref], "calculation_ids": []},
            ],
            "gaps": 1, "competing_accounts": 42,
        }))


def test_pr586_regressions_premise_grounding_summary_signs_ollama_and_malformed_fields():
    # 1. Fabricated calculation premise + uninspected citation must be rejected.
    rt_fake = MatterToolRuntime("matter-stock", "defender-1", _stock_backend())
    res_fake = CaseAssistant(_FakeCalcModel()).investigate("Check ownership", rt_fake)
    assert res_fake.answerable is False and res_fake.findings == () and res_fake.omitted_findings == 1
    assert "87.65" not in res_fake.summary

    # 2. Unverified number in summary cannot leak past verified findings.
    res_sum = CaseAssistant(_BadSummaryModel()).investigate("Ownership", MatterToolRuntime("matter-stock", "defender-1", _stock_backend()))
    assert res_sum.answerable is True and "99.99" not in res_sum.summary and "%" in res_sum.summary

    # 3. Signed numeric verification: negative subtraction result passes; sign reversal fails.
    res_sign = CaseAssistant(_SignModel()).investigate("Net balance?", MatterToolRuntime("m1", "def", _LedgerBackend()))
    assert res_sign.answerable is True and len(res_sign.findings) == 1 and res_sign.findings[0].text == "Balance is -5."
    assert res_sign.omitted_findings == 1  # sign reversal rejected; non-iterable gaps/competing_accounts did not crash

    # 4. Native Ollama multi-turn sends dict arguments (not JSON string) on turn 2.
    ollama_multi = _FakeOpener([
        {"message": {"content": "", "tool_calls": [{"function": {"name": "list_sources", "arguments": {"limit": 1}}}]}},
        {"message": {"content": json.dumps({"answerable": False, "summary": "No match found.", "findings": [], "gaps": [], "competing_accounts": []})}},
    ])
    client = OllamaAssistantClient("http://127.0.0.1:8790", "synthetic-model", opener=ollama_multi)
    CaseAssistant(client, tool_style="ollama").investigate("List sources", MatterToolRuntime("matter-stock", "defender-1", _stock_backend()))
    assert len(ollama_multi.calls) == 2
    second_messages = ollama_multi.calls[1]["messages"]
    assistant_history = [m for m in second_messages if m.get("role") == "assistant"][0]
    assert isinstance(assistant_history["tool_calls"][0]["function"]["arguments"], dict)
    assert assistant_history["tool_calls"][0]["function"]["arguments"] == {"limit": 1}


class _VersionedAndManyFailedBackend:
    def __init__(self) -> None:
        self.read_count = 0

    def authorize(self, m, a): return True

    def list_sources(self, m, *, status="", offset=0, limit=50, **kw):
        from case_intelligence.case_assistant_tools import SourceSummary
        failed = [SourceSummary(f"f-{i}", f"failed-{i}.pdf", "pdf", "failed", 0) for i in range(1, 31)]
        if status == "failed":
            return failed[offset:offset + limit], len(failed)
        ready = [SourceSummary("doc-v", "bal.txt", "txt", "ready", 1)]
        rows = ready + failed
        return rows[offset:offset + limit], len(rows)

    def search_discovery(self, m, q, **kw): return ()

    def read_source(self, m, d, **kw):
        from case_intelligence.case_assistant_tools import SourcePage, SourceReadResult
        self.read_count += 1
        if self.read_count == 1:
            return SourceReadResult("doc-v", "bal.txt", 1, (SourcePage(1, "Page 1", "Balance 10", "v1"),))
        return SourceReadResult("doc-v", "bal.txt", 1, (SourcePage(1, "Page 1", "Balance 15", "v2"),))

    def get_case_knowledge(self, m, **kw): return {}


class _Round2VerifierModel:
    def __init__(self) -> None:
        self.step = 0

    def complete_turn(self, messages, *, tools):
        self.step += 1
        if self.step == 1:
            return AssistantTurn(tool_calls=(
                AssistantModelCall("r1", "list_sources", {"limit": 5}),
                AssistantModelCall("r2", "read_source", {"document_id": "doc-v", "start_page": 1}),
            ))
        if self.step == 2:
            return AssistantTurn(tool_calls=(
                AssistantModelCall("r3", "read_source", {"document_id": "doc-v", "start_page": 1}),
                AssistantModelCall("r4", "calculate", {
                    "operation": "sum", "inputs": [{
                        "label": "fabricated 87.65", "value": "10",
                        "source_refs": [{"document_id": "doc-v", "source_name": "bal.txt", "location": "Page 1", "source_version_id": "v1"}],
                    }],
                }),
            ))
        v1 = {"document_id": "doc-v", "source_name": "bal.txt", "location": "Page 1", "source_version_id": "v1"}
        v2 = {"document_id": "doc-v", "source_name": "bal.txt", "location": "Page 1", "source_version_id": "v2"}
        return AssistantTurn(content=json.dumps({
            "answerable": True,
            "summary": "Balance 10",
            "findings": [
                {"text": "Balance 10", "citations": [v1], "calculation_ids": []},
                {"text": "Balance 10", "citations": [v2], "calculation_ids": []},
                {"text": "Balance 87.65", "citations": [v1], "calculation_ids": ["calc-1"]},
            ],
            "gaps": [], "competing_accounts": [],
        }))


def test_pr586_round2_label_laundering_version_isolation_and_unavailable_total():
    rt = MatterToolRuntime("m1", "def", _VersionedAndManyFailedBackend())
    res = CaseAssistant(_Round2VerifierModel()).investigate("Check balances", rt)
    # Only the v1-cited Balance 10 passes; v2 mismatch and label-laundered 87.65 are omitted.
    assert res.answerable is True and len(res.findings) == 1 and res.omitted_findings == 2
    assert res.findings[0].citations[0].source_version_id == "v1"
    # Full unavailable count (30) is preserved even though sample list caps at 25.
    assert any("30 unavailable/unreadable source(s)" in g for g in res.gaps)


class _SearchOnlyMetadataBackend:
    def authorize(self, m, a): return True
    def list_sources(self, m, **kw): return ([], 0)
    def search_discovery(self, m, q, **kw):
        from case_intelligence.case_assistant_tools import SearchHit, SearchResponse
        return SearchResponse(
            hits=(SearchHit("d1", "87.65.pdf", "Page 1", "Balance 10", 1.0, "v1"),),
            total_matches=1, offset=0, has_more=False, excluded_unavailable_count=30,
        )
    def read_source(self, m, d, **kw): return None
    def get_case_knowledge(self, m, **kw): return {}


class _MetadataAndSciNotationModel:
    def __init__(self) -> None:
        self.step = 0
    def complete_turn(self, messages, *, tools):
        self.step += 1
        if self.step == 1:
            return AssistantTurn(tool_calls=(AssistantModelCall("m1", "search_discovery", {"query": "Balance", "limit": 5}),))
        if self.step == 2:
            ref = {"document_id": "d1", "source_name": "87.65.pdf", "location": "Page 1", "source_version_id": "v1"}
            return AssistantTurn(tool_calls=(AssistantModelCall("m2", "calculate", {
                "operation": "sum", "inputs": [{"label": "x", "value": "87.65", "source_refs": [ref]}],
            }),))
        ref = {"document_id": "d1", "source_name": "87.65.pdf", "location": "Page 1", "source_version_id": "v1"}
        return AssistantTurn(content=json.dumps({
            "answerable": True,
            "summary": "Balance is 8765e99.",
            "findings": [
                {"text": "Balance is 87.65.", "citations": [ref], "calculation_ids": ["calc-1"]},
                {"text": "Balance is 8765e99.", "citations": [ref], "calculation_ids": []},
            ],
            "gaps": [], "competing_accounts": [],
        }))


def test_pr586_round3_filename_numbers_sci_notation_and_search_unavailable_count():
    rt = MatterToolRuntime("m1", "def", _SearchOnlyMetadataBackend())
    res = CaseAssistant(_MetadataAndSciNotationModel()).investigate("Check balance", rt)
    # Neither filename-derived 87.65 nor scientific notation 8765e99 may pass verification.
    assert res.answerable is False and res.findings == () and res.omitted_findings == 2
    assert any("30 unavailable/unreadable source(s)" in g for g in res.gaps)


class _DecimalAndRunningBackend:
    def authorize(self, m, a): return True
    def list_sources(self, m, **kw): return ([], 0)
    def search_discovery(self, m, q, **kw): return ()
    def read_source(self, m, d, **kw):
        from case_intelligence.case_assistant_tools import SourcePage, SourceReadResult
        return SourceReadResult("d-rb", "tx.txt", 1, (
            SourcePage(1, "Page 1", "Transactions: 10, 15, -5; fee 5.", "v1"),
        ))
    def get_case_knowledge(self, m, **kw): return {}


class _DecimalAndRunningModel:
    def __init__(self) -> None:
        self.step = 0
    def complete_turn(self, messages, *, tools):
        self.step += 1
        ref = {"document_id": "d-rb", "source_name": "tx.txt", "location": "Page 1", "source_version_id": "v1"}
        if self.step == 1:
            return AssistantTurn(tool_calls=(AssistantModelCall("rb1", "read_source", {"document_id": "d-rb", "start_page": 1}),))
        if self.step == 2:
            return AssistantTurn(tool_calls=(AssistantModelCall("rb2", "calculate", {
                "operation": "running_balance",
                "inputs": [
                    {"label": "t0", "value": "10", "source_refs": [ref]},
                    {"label": "t1", "value": "15", "source_refs": [ref]},
                    {"label": "t2", "value": "-5", "source_refs": [ref]},
                ],
            }),))
        return AssistantTurn(content=json.dumps({
            "answerable": True,
            "summary": "Peak running balance was 25.",
            "findings": [
                {"text": "The running balance after the second transaction was 25.", "citations": [ref], "calculation_ids": ["calc-1"]},
                {"text": "Fee was .5.", "citations": [ref], "calculation_ids": []},
            ],
            "gaps": [], "competing_accounts": [],
        }))


def test_pr586_leading_dot_and_running_balance_intermediates():
    rt = MatterToolRuntime("m1", "def", _DecimalAndRunningBackend())
    res = CaseAssistant(_DecimalAndRunningModel()).investigate("Running peak?", rt)
    # Verified intermediate balance 25 passes; .5 against source 5 is rejected.
    assert res.answerable is True and len(res.findings) == 1 and "25" in res.findings[0].text
    assert res.omitted_findings == 1




