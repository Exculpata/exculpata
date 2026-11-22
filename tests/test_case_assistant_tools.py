"""Synthetic proving and fail-closed tests for the Case Assistant tool runtime."""
from __future__ import annotations

from dataclasses import dataclass
import re

import pytest

from case_intelligence.case_assistant_tools import (
    AssistantAccessError, AssistantToolError, MatterToolRuntime, SearchHit,
    SourcePage, SourceReadResult, SourceSummary, ollama_tool_schemas,
    openai_tool_schemas, run_calculation,
)
from case_intelligence.stock_case_gold import build_case
from case_intelligence.stock_case_render import documents as render_stock_documents


@dataclass(frozen=True)
class _SyntheticDoc:
    matter_id: str; summary: SourceSummary; pages: tuple[SourcePage, ...]

class _SyntheticMatterBackend:
    def __init__(self, docs: list[_SyntheticDoc], *, authorized: set[tuple[str, str]], knowledge: dict[str, dict[str, object]]) -> None:
        self.docs, self.authorized, self.knowledge, self.calls = {d.summary.document_id: d for d in docs}, authorized, knowledge, []
    def authorize(self, matter_id: str, actor_id: str) -> bool: return (matter_id, actor_id) in self.authorized
    def list_sources(self, matter_id: str, *, query: str = "", kind: str = "", status: str = "", offset: int = 0, limit: int = 50):
        self.calls.append("list_sources")
        rows = [d.summary for d in self.docs.values() if d.matter_id == matter_id and (not kind or d.summary.kind == kind)
                and (not status or d.summary.status == status) and (not query or query.casefold() in (d.summary.source_name + d.summary.bates).casefold())]
        return rows[offset:offset + limit], len(rows)
    def search_discovery(self, matter_id: str, query: str, *, mode: str = "hybrid", limit: int = 20, document_ids: frozenset[str] | None = None):
        self.calls.append(f"search_discovery:{mode}")
        terms = [t for t in re.findall(r"[a-z0-9_-]+", query.casefold()) if len(t) > 1]
        hits = [SearchHit(d.summary.document_id, d.summary.source_name, p.location, p.text,
                          float(sum(1 for t in terms if t in p.text.casefold())), p.source_version_id)
                for d in self.docs.values() if d.matter_id == matter_id and d.summary.status == "ready"
                and (document_ids is None or d.summary.document_id in document_ids)
                for p in d.pages if any(t in p.text.casefold() for t in terms)]
        hits.sort(key=lambda item: (-item.score, item.document_id, item.location))
        return hits[:limit]
    def read_source(self, matter_id: str, document_id: str, *, start_page: int = 1, end_page: int | None = None):
        self.calls.append(f"read_source:{document_id}")
        doc = self.docs.get(document_id)
        if doc is None or doc.matter_id != matter_id: return None
        stop = len(doc.pages) if end_page is None else min(end_page, len(doc.pages))
        return SourceReadResult(doc.summary.document_id, doc.summary.source_name, len(doc.pages),
                                tuple(p for p in doc.pages if start_page <= p.page_number <= stop))
    def get_case_knowledge(self, matter_id: str, *, section: str = "all", limit: int = 50):
        self.calls.append(f"get_case_knowledge:{section}")
        data = self.knowledge.get(matter_id, {})
        return data if section == "all" else {section: data.get(section, [])}

def _stock_backend() -> _SyntheticMatterBackend:
    docs = [_SyntheticDoc("matter-stock", SourceSummary(f"stock-{i:06}", f"SPND-{i:06}-{kind}.{ext}", kind, "ready", len(body), bates=f"SPND-{i:06}"),
                          tuple(SourcePage(n, f"Page {n}", text, f"v1-stock-{i:06}") for n, text in enumerate(body, 1)))
            for i, (kind, ext, body) in enumerate(render_stock_documents(build_case(seed=7)), 1)]
    docs.extend((
        _SyntheticDoc("matter-stock", SourceSummary("stock-damaged", "damaged-scan.pdf", "pdf", "failed", 0, status_detail="Unreadable image stream"), ()),
        _SyntheticDoc("matter-other", SourceSummary("other-001", "other-matter.txt", "txt", "ready", 1),
                      (SourcePage(1, "Page 1", "Separate matter synthetic record.", "v1-other"),)),
    ))
    return _SyntheticMatterBackend(docs, authorized={("matter-stock", "defender-1")},
                                   knowledge={"matter-stock": {"case_theory": [{"claim": "Ownership exceeded 10% after buyback."}]}})


def test_stock_ownership_end_to_end_tool_sequence_and_exact_math():
    """VISION.md proving test: discover filings + statements, compute >10% with receipts."""
    runtime = MatterToolRuntime("matter-stock", "defender-1", _stock_backend())
    inventory = runtime.invoke("list_sources", {"limit": 10})
    assert inventory["total"] == 406 and inventory["unavailable_count"] == 1
    assert inventory["unavailable_sources"][0]["document_id"] == "stock-damaged"

    filing_hits = runtime.invoke("search_discovery", {"query": "buyback 2024-09-01 shares", "limit": 5})
    buyback_hit = next(h for h in filing_hits["hits"] if "event=buyback" in h["excerpt"])
    filing_doc = runtime.invoke("read_source", {"document_id": buyback_hit["document_id"], "start_page": 1, "end_page": 2})
    buyback_shares = re.search(r"shares:\s*(\d+)", filing_doc["pages"][1]["text"]).group(1)

    position_inputs = []
    for account_id in ("stk-0", "stk-1", "stk-2", "stk-3"):
        hits = runtime.invoke("search_discovery", {"query": f"Position 2024-09-30 {account_id}", "limit": 5})
        match = next(h for h in hits["hits"] if f"date=2024-09-30; account={account_id}" in h["excerpt"])
        position_inputs.append({
            "label": account_id, "value": re.search(r"shares=(\d+)", match["excerpt"]).group(1),
            "source_refs": [{k: match[k] for k in ("document_id", "source_name", "location", "source_version_id")}],
        })

    combined = runtime.invoke("calculate", {"operation": "sum", "inputs": position_inputs, "unit": "shares"})
    pct = runtime.invoke("calculate", {"operation": "percent", "inputs": [
        {"label": "combined_controlled_shares", "value": combined["result"],
         "source_refs": [ref for item in position_inputs for ref in item["source_refs"]]},
        {"label": "outstanding_post_buyback", "value": buyback_shares,
         "source_refs": [{"document_id": buyback_hit["document_id"], "source_name": buyback_hit["source_name"], "location": "Page 2"}]},
    ]})
    assert float(pct["result"]) > 10.0 and pct["unit"] == "%"
    assert len(runtime.receipts) == 2 and len(runtime.cited_refs) >= 5
    assert runtime.invoke("get_case_knowledge", {"section": "case_theory"})["case_theory"]


def test_fail_closed_authorization_isolation_and_invalid_inputs():
    backend = _stock_backend()
    with pytest.raises(AssistantAccessError):
        MatterToolRuntime("matter-stock", "intruder-1", backend).invoke("list_sources", {})
    assert backend.calls == []

    runtime = MatterToolRuntime("matter-stock", "defender-1", backend)
    with pytest.raises(AssistantAccessError):
        runtime.invoke("read_source", {"document_id": "other-001"})
    with pytest.raises(AssistantToolError):
        runtime.invoke("run_shell", {"cmd": "id"})
    with pytest.raises(AssistantToolError):
        run_calculation("divide", inputs=[{"label": "a", "value": "10"}, {"label": "b", "value": "0"}])
    with pytest.raises(AssistantToolError):
        run_calculation("sum", inputs=[{"label": "bad", "value": "NaN"}])

    days = run_calculation("days_between", inputs=[{"label": "buyback", "value": "2024-09-01"}, {"label": "sale", "value": "2024-10-15"}])
    assert days.result == "44" and days.unit == "days"


def test_tool_schemas_are_strictly_read_only_and_allowlisted():
    expected = {
        "list_sources", "search_discovery", "read_source", "get_case_knowledge",
        "calculate", "query_table", "read_email_thread", "read_transcript_window",
    }
    assert {entry["function"]["name"] for entry in openai_tool_schemas()} == expected
    assert {entry["function"]["name"] for entry in ollama_tool_schemas()} == expected
