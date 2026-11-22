"""Synthetic proving and fail-closed tests for the Case Assistant tool runtime."""
from __future__ import annotations

from dataclasses import dataclass
import re

import pytest

from case_intelligence.case_assistant_tools import (
    AssistantAccessError, AssistantToolError, MatterToolRuntime, SearchHit,
    SearchResponse, SourcePage, SourceReadResult, SourceSummary, ollama_tool_schemas,
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
    def list_sources(self, matter_id: str, *, query: str = "", kind: str = "", status: str = "", offset: int = 0, limit: int = 50,
                     document_ids: frozenset[str] | None = None):
        self.calls.append("list_sources")
        rows = [d.summary for d in self.docs.values() if d.matter_id == matter_id
                and (document_ids is None or d.summary.document_id in document_ids)
                and (not kind or d.summary.kind == kind)
                and (not status or d.summary.status == status)
                and (not query or query.casefold() in (d.summary.source_name + d.summary.bates).casefold())]
        return rows[offset:offset + limit], len(rows)
    def search_discovery(self, matter_id: str, query: str, *, mode: str = "hybrid", limit: int = 20, offset: int = 0,
                         document_ids: frozenset[str] | None = None):
        self.calls.append(f"search_discovery:{mode}")
        terms = [t for t in re.findall(r"[a-z0-9_-]+", query.casefold()) if len(t) > 1]
        excluded_unavail = sum(
            1 for d in self.docs.values()
            if d.matter_id == matter_id and d.summary.status != "ready"
            and (document_ids is None or d.summary.document_id in document_ids)
        )
        hits = [SearchHit(d.summary.document_id, d.summary.source_name, p.location, p.text,
                          float(sum(1 for t in terms if t in p.text.casefold())), p.source_version_id)
                for d in self.docs.values() if d.matter_id == matter_id and d.summary.status == "ready"
                and (document_ids is None or d.summary.document_id in document_ids)
                for p in d.pages if any(t in p.text.casefold() for t in terms)]
        hits.sort(key=lambda item: (-item.score, item.document_id, item.location))
        page = tuple(hits[offset:offset + limit])
        return SearchResponse(hits=page, total_matches=len(hits), offset=offset,
                              has_more=(offset + len(page) < len(hits)),
                              excluded_unavailable_count=excluded_unavail)
    def read_source(self, matter_id: str, document_id: str, *, start_page: int = 1, end_page: int | None = None):
        self.calls.append(f"read_source:{document_id}")
        doc = self.docs.get(document_id)
        if doc is None or doc.matter_id != matter_id: return None
        stop = len(doc.pages) if end_page is None else min(end_page, len(doc.pages))
        return SourceReadResult(doc.summary.document_id, doc.summary.source_name, len(doc.pages),
                                tuple(p for p in doc.pages if start_page <= p.page_number <= stop))
    def get_case_knowledge(self, matter_id: str, *, section: str = "all", limit: int = 50,
                           document_ids: frozenset[str] | None = None):
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


def test_scoped_inventory_knowledge_filters_pagination_continuation_decimal_and_immutable_trace():
    """Regressions for all PR #585 review findings."""
    big_text = ("A" * 1_200) + "MATERIAL_TAIL_FACT"
    docs = [
        _SyntheticDoc("m1", SourceSummary("d-1", "doc1.txt", "txt", "ready", 1), (SourcePage(1, "Page 1", "alpha common", "v1"),)),
        _SyntheticDoc("m1", SourceSummary("d-2", "doc2.txt", "txt", "ready", 1), (SourcePage(1, "Page 1", "beta common", "v1"),)),
        _SyntheticDoc("m1", SourceSummary("d-3", "doc3.txt", "txt", "ready", 1), (SourcePage(1, "Page 1", big_text, "v1"),)),
        _SyntheticDoc("m1", SourceSummary("d-fail", "bad.pdf", "pdf", "failed", 0, status_detail="corrupt"), ()),
    ]
    backend = _SyntheticMatterBackend(
        docs, authorized={("m1", "def")},
        knowledge={"m1": {"case_theory": [{"claim": "Unscoped theory"}]}},
    )

    # 1. Scoped list_sources excludes out-of-scope failed sources and respects empty scope.
    scoped = MatterToolRuntime("m1", "def", backend, allowed_document_ids=frozenset({"d-2"}))
    inv = scoped.invoke("list_sources", {"limit": 10})
    assert inv["total"] == 1 and [s["document_id"] for s in inv["sources"]] == ["d-2"]
    assert inv["unavailable_count"] == 0 and inv["unavailable_sources"] == []
    empty_inv = MatterToolRuntime("m1", "def", backend, allowed_document_ids=frozenset()).invoke("list_sources", {})
    assert empty_inv["total"] == 0 and empty_inv["unavailable_count"] == 0

    # 2. Scoped get_case_knowledge fails closed when items lack complete in-scope support,
    #    and succeeds when all referenced document_ids belong to allowed_document_ids.
    with pytest.raises(AssistantAccessError):
        scoped.invoke("get_case_knowledge", {"section": "case_theory"})
    backend.knowledge["m1"]["case_theory"] = [{"claim": "Scoped theory", "source_document_ids": ["d-2"]}]
    assert scoped.invoke("get_case_knowledge", {"section": "case_theory"})["case_theory"][0]["claim"] == "Scoped theory"

    # 3. Malformed document_ids must fail closed instead of widening search.
    rt = MatterToolRuntime("m1", "def", backend, max_read_chars=1_000)
    with pytest.raises(AssistantToolError):
        rt.invoke("search_discovery", {"query": "common", "document_ids": "d-1"})
    with pytest.raises(AssistantToolError):
        rt.invoke("search_discovery", {"query": "common", "document_ids": [123]})
    assert rt.invoke("search_discovery", {"query": "common", "document_ids": []})["count"] == 0

    # 4. Exact search exposes pagination + total + excluded unavailable coverage metadata.
    p1 = rt.invoke("search_discovery", {"query": "common", "mode": "exact", "limit": 1, "offset": 0})
    assert p1["total_matches"] == 2 and p1["has_more"] is True and p1["next_offset"] == 1
    assert p1["excluded_unavailable_count"] == 1
    p2 = rt.invoke("search_discovery", {"query": "common", "mode": "exact", "limit": 1, "offset": p1["next_offset"]})
    assert p2["count"] == 1 and p2["has_more"] is False and p2["next_offset"] is None
    assert p1["hits"][0]["document_id"] != p2["hits"][0]["document_id"]

    # 5. Oversized page continuation via char_offset reaches the tail.
    head = rt.invoke("read_source", {"document_id": "d-3", "start_page": 1, "end_page": 1})
    assert head["truncated"] is True and head["next_char_offset"] == 1_000
    tail = rt.invoke("read_source", {"document_id": "d-3", "start_page": 1, "end_page": 1, "char_offset": head["next_char_offset"]})
    assert tail["truncated"] is False and "MATERIAL_TAIL_FACT" in tail["pages"][0]["text"]

    # 6. Isolated Decimal precision + comma grouping validation + malformed source_refs rejection.
    huge_sum = run_calculation("sum", inputs=[
        {"label": "a", "value": "10000000000000000000000000000"},
        {"label": "b", "value": "1"},
        {"label": "c", "value": "-10000000000000000000000000000"},
    ])
    assert huge_sum.result == "1"
    assert run_calculation("sum", inputs=[{"label": "big", "value": "10000000000000000000000"}]).result == "10000000000000000000000"
    assert run_calculation("sum", inputs=[{"label": "grouped", "value": "1,234,567.50"}]).result == "1234567.5"
    for bad_comma in ("1,2", ",5", "1,,000", "1,000,00"):
        with pytest.raises(AssistantToolError):
            run_calculation("sum", inputs=[{"label": "bad", "value": bad_comma}])
    with pytest.raises(AssistantToolError):
        run_calculation("sum", inputs=[{"label": "badref", "value": "10", "source_refs": [42]}])

    # 7. Audit trace stores immutable deep snapshot of arguments.
    calc_args = {"operation": "sum", "inputs": [{"label": "x", "value": "10"}]}
    rt.invoke("calculate", calc_args)
    calc_args["inputs"][0]["value"] = "999"
    assert rt.trace[-1].arguments["inputs"][0]["value"] == "10"
    with pytest.raises(TypeError):
        rt.trace[-1].arguments["operation"] = "divide"  # type: ignore[index]

    # 8. Document-restricted runtime rejects calculate references outside allowed_document_ids.
    empty_rt = MatterToolRuntime("m1", "def", backend, allowed_document_ids=frozenset())
    with pytest.raises(AssistantAccessError):
        empty_rt.invoke("calculate", {"operation": "sum", "inputs": [
            {"label": "x", "value": "10", "source_refs": [{"document_id": "d-1", "source_name": "doc1.txt", "location": "Page 1"}]},
        ]})
    assert empty_rt.receipts == {} and empty_rt.cited_refs == ()

