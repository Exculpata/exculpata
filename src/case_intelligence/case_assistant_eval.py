"""Repeatable synthesis evaluation runner for the multi-turn Case Assistant."""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
import re
import sys
from typing import Mapping, Sequence

from .case_assistant import (
    AssistantInvestigationResult, AssistantModelCall, AssistantTurn,
    CaseAssistant, OllamaAssistantClient, OpenAIAssistantClient,
)
from .case_assistant_tools import (
    MatterToolRuntime, SearchHit, SourcePage, SourceReadResult, SourceSummary,
)
from .stock_case_gold import build_case
from .stock_case_render import documents as render_stock_documents


@dataclass(frozen=True)
class EvalCase:
    case_id: str
    question: str
    expected_answerable: bool
    expected_value: str | None = None
    tolerance: float = 0.05
    require_receipt: bool = False
    required_source_kinds: tuple[str, ...] = ()
    require_gap_notice: bool = False
    require_competing_accounts: bool = False


@dataclass(frozen=True)
class _EvalDoc:
    summary: SourceSummary
    pages: tuple[SourcePage, ...]


class InMemoryEvalBackend:
    """Deterministic MatterToolBackend backed by a rendered synthetic discovery corpus."""

    def __init__(self, matter_id: str, docs: Sequence[_EvalDoc]) -> None:
        self.matter_id = matter_id
        self.docs = {d.summary.document_id: d for d in docs}

    def authorize(self, matter_id: str, actor_id: str) -> bool:
        return matter_id == self.matter_id

    def list_sources(self, matter_id: str, *, query: str = "", kind: str = "",
                     status: str = "", offset: int = 0, limit: int = 50):
        rows = [d.summary for d in self.docs.values()
                if (not kind or d.summary.kind == kind) and (not status or d.summary.status == status)
                and (not query or query.casefold() in (d.summary.source_name + d.summary.bates).casefold())]
        return rows[offset:offset + limit], len(rows)

    def search_discovery(self, matter_id: str, query: str, *, mode: str = "hybrid",
                         limit: int = 20, document_ids: frozenset[str] | None = None):
        terms = [t for t in re.findall(r"[a-z0-9_-]+", query.casefold()) if len(t) > 1]
        hits = [
            SearchHit(d.summary.document_id, d.summary.source_name, p.location, p.text,
                      float(sum(1 for t in terms if t in p.text.casefold())), p.source_version_id)
            for d in self.docs.values()
            if d.summary.status == "ready" and (document_ids is None or d.summary.document_id in document_ids)
            for p in d.pages if any(t in p.text.casefold() for t in terms)
        ]
        hits.sort(key=lambda item: (-item.score, item.document_id, item.location))
        return hits[:limit]

    def read_source(self, matter_id: str, document_id: str, *, start_page: int = 1, end_page: int | None = None):
        doc = self.docs.get(document_id)
        if doc is None:
            return None
        stop = len(doc.pages) if end_page is None else min(end_page, len(doc.pages))
        return SourceReadResult(doc.summary.document_id, doc.summary.source_name, len(doc.pages),
                                tuple(p for p in doc.pages if start_page <= p.page_number <= stop))

    def get_case_knowledge(self, matter_id: str, *, section: str = "all", limit: int = 50) -> Mapping[str, object]:
        return {}


def build_stock_eval_suite(seed: int = 7) -> tuple[tuple[EvalCase, ...], InMemoryEvalBackend]:
    """Build the seeded 405-document stock corpus + unreadable scan and exact answer key."""
    case = build_case(seed=seed)
    docs = [
        _EvalDoc(
            SourceSummary(f"stock-{i:06}", f"SPND-{i:06}-{kind}.{ext}", kind, "ready", len(body), bates=f"SPND-{i:06}"),
            tuple(SourcePage(n, f"Page {n}", text, f"v1-stock-{i:06}") for n, text in enumerate(body, 1)),
        )
        for i, (kind, ext, body) in enumerate(render_stock_documents(case), 1)
    ]
    unreadable_name = Path("damaged-scan.pdf").name
    docs.append(_EvalDoc(SourceSummary("stock-damaged", unreadable_name, "pdf", "failed", 0, status_detail="Unreadable image stream"), ()))
    buyback_shares = next(r["shares"] for r in case["share_history"] if r["event"] == "buyback")
    controlled_sep = sum(
        s["shares"] for s in case["statements"]
        if s["date"] == "2024-09-30" and s["account_id"] in case["combined_account_ids"]
    )
    expected_pct = format((Decimal(controlled_sep) / Decimal(buyback_shares) * Decimal(100)).quantize(Decimal("0.01")), "f")
    suite = (
        EvalCase(
            "ownership_threshold",
            "Did the subject or controlled accounts ever own more than 10% of Orbel Spindle Works stock? Show combined shares, post-buyback share count, exact percentage, and note any unreadable files.",
            expected_answerable=True, expected_value=expected_pct, tolerance=0.05,
            require_receipt=True, required_source_kinds=("filing", "statement"), require_gap_notice=True,
        ),
        EvalCase(
            "buyback_to_sale_days",
            "How many calendar days elapsed between the 2024-09-01 share buyback and the 2024-10-15 share sales?",
            expected_answerable=True, expected_value="44", tolerance=0.0,
            require_receipt=True, required_source_kinds=("filing", "confirmation"),
        ),
        EvalCase(
            "associate_contradiction",
            "Are the associate's interview statements consistent with the email record regarding trading instructions?",
            expected_answerable=True, required_source_kinds=("interview", "email"), require_competing_accounts=True,
        ),
        EvalCase(
            "unrecorded_market_price_trap",
            "What was the closing market price per share of Orbel Spindle Works on September 30, 2024?",
            expected_answerable=False,
        ),
    )
    return suite, InMemoryEvalBackend(case["matter_id"], docs)


def _matches_numeric(target: str, candidates: Sequence[str], tolerance: float) -> bool:
    try:
        expected = Decimal(target)
    except InvalidOperation:
        return False
    tol = Decimal(str(tolerance))
    for raw in candidates:
        for token in re.findall(r"-?\d+(?:\.\d+)?", raw.replace(",", "")):
            try:
                if abs(Decimal(token) - expected) <= tol:
                    return True
            except InvalidOperation:
                continue
    return False


def score_eval_case(case: EvalCase, result: AssistantInvestigationResult) -> dict[str, object]:
    """Deterministically score one Case Assistant investigation out of 100 points."""
    penalties: list[str] = []
    if not case.expected_answerable:
        if result.answerable:
            return {"case_id": case.case_id, "score": 0.0, "penalties": ["trap_false_answer"],
                    "turns_used": result.turns_used, "receipts": len(result.receipts)}
        return {"case_id": case.case_id, "score": 100.0, "penalties": [],
                "turns_used": result.turns_used, "receipts": len(result.receipts)}

    accuracy, citations_pts, math_pts, honesty_pts = 0.0, 0.0, 0.0, 15.0
    if result.answerable:
        if case.expected_value is None:
            accuracy = 40.0
        else:
            texts = [result.summary, *(f.text for f in result.findings), *(r.result for r in result.receipts.values())]
            if _matches_numeric(case.expected_value, texts, case.tolerance):
                accuracy = 40.0
            else:
                penalties.append("numeric_mismatch")
    else:
        penalties.append("false_abstention")

    cited_names = [ref.source_name.casefold() for f in result.findings for ref in f.citations]
    if case.required_source_kinds:
        matched = sum(1 for kind in case.required_source_kinds if any(f"-{kind}." in name for name in cited_names))
        citations_pts = round(25.0 * (matched / len(case.required_source_kinds)), 2)
        if matched < len(case.required_source_kinds):
            penalties.append("incomplete_source_kinds")
    else:
        citations_pts = 25.0 if cited_names else 0.0

    if case.require_receipt:
        receipt_vals = [r.result for r in result.receipts.values()]
        if case.expected_value and _matches_numeric(case.expected_value, receipt_vals, case.tolerance):
            math_pts = 20.0
        else:
            penalties.append("missing_calculation_receipt")
    else:
        math_pts = 20.0

    if result.omitted_findings > 0:
        honesty_pts = max(0.0, honesty_pts - 5.0 * result.omitted_findings)
        penalties.append("omitted_unverified_findings")
    unreadable_name = Path("damaged-scan.pdf").name
    if case.require_gap_notice and not any(unreadable_name in g or "unavailable" in g.casefold() for g in result.gaps):
        honesty_pts = max(0.0, honesty_pts - 7.5)
        penalties.append("missed_unreadable_gap")
    if case.require_competing_accounts and not result.competing_accounts:
        honesty_pts = max(0.0, honesty_pts - 7.5)
        penalties.append("missed_competing_accounts")

    total = round(accuracy + citations_pts + math_pts + honesty_pts, 2)
    return {
        "case_id": case.case_id, "score": total, "penalties": penalties,
        "turns_used": result.turns_used, "receipts": len(result.receipts),
        "verified_findings": len(result.findings), "omitted_findings": result.omitted_findings,
    }


class _ScriptedEvalModel:
    """Reference synthetic model proving the full 4-case stock suite end to end."""

    def __init__(self) -> None:
        self._step = 0

    def complete_turn(self, messages, *, tools):
        q = next(m["content"] for m in messages if m["role"] == "user")
        tool_msgs = [json.loads(m["content"]) for m in messages if m.get("role") == "tool"]
        if "closing market price" in q:
            if not tool_msgs:
                return AssistantTurn(tool_calls=(AssistantModelCall("t1", "search_discovery", {"query": "closing market price share", "limit": 5}),))
            return AssistantTurn(content=json.dumps({
                "answerable": False, "summary": "The discovery contains no closing market price records.",
                "findings": [], "gaps": ["No market pricing data appears in the matter records."], "competing_accounts": [],
            }))
        if "calendar days elapsed" in q:
            if not tool_msgs:
                return AssistantTurn(tool_calls=(
                    AssistantModelCall("d1", "search_discovery", {"query": "buyback 2024-09-01", "limit": 5}),
                    AssistantModelCall("d2", "search_discovery", {"query": "2024-10-15 stk-0", "limit": 5}),
                ))
            if not any("operation" in m for m in tool_msgs):
                buy = next(h for m in tool_msgs for h in m.get("hits", ()) if "-filing." in h["source_name"])
                sale = next(h for m in tool_msgs for h in m.get("hits", ()) if "-confirmation." in h["source_name"])
                return AssistantTurn(tool_calls=(AssistantModelCall("d3", "calculate", {"operation": "days_between", "inputs": [
                    {"label": "buyback", "value": "2024-09-01", "source_refs": [{k: buy[k] for k in ("document_id", "source_name", "location")}]},
                    {"label": "sale", "value": "2024-10-15", "source_refs": [{k: sale[k] for k in ("document_id", "source_name", "location")}]},
                ]}),))
            buy = next(h for m in tool_msgs for h in m.get("hits", ()) if "-filing." in h["source_name"])
            sale = next(h for m in tool_msgs for h in m.get("hits", ()) if "-confirmation." in h["source_name"])
            return AssistantTurn(content=json.dumps({
                "answerable": True, "summary": "44 calendar days elapsed between 2024-09-01 and 2024-10-15.",
                "findings": [{"text": "44 days elapsed between the 2024-09-01 buyback and the 2024-10-15 sales.",
                              "citations": [{k: buy[k] for k in ("document_id", "source_name", "location")},
                                            {k: sale[k] for k in ("document_id", "source_name", "location")}],
                              "calculation_ids": ["calc-1"]}],
                "gaps": [], "competing_accounts": [],
            }))
        if "interview statements consistent" in q:
            if not tool_msgs:
                return AssistantTurn(tool_calls=(
                    AssistantModelCall("i1", "search_discovery", {"query": "never received trading instructions", "limit": 5}),
                    AssistantModelCall("i2", "search_discovery", {"query": "Order shares stk-4 tomorrow", "limit": 5}),
                ))
            iv = next(h for m in tool_msgs for h in m.get("hits", ()) if "-interview." in h["source_name"])
            em = next(h for m in tool_msgs for h in m.get("hits", ()) if "-email." in h["source_name"])
            return AssistantTurn(content=json.dumps({
                "answerable": True, "summary": "No. The associate's interviews contradict the email instructions.",
                "findings": [{"text": "An interview states 'I never received trading instructions' while emails send monthly share orders for stk-4.",
                              "citations": [{k: iv[k] for k in ("document_id", "source_name", "location")},
                                            {k: em[k] for k in ("document_id", "source_name", "location")}],
                              "calculation_ids": []}],
                "gaps": [], "competing_accounts": ["Interview denies trading instructions while emails instruct stk-4 share orders."],
            }))
        # Default: canonical ownership_threshold case
        if not tool_msgs:
            return AssistantTurn(tool_calls=(
                AssistantModelCall("o1", "list_sources", {"limit": 10}),
                AssistantModelCall("o2", "search_discovery", {"query": "buyback 2024-09-01 shares", "limit": 5}),
                *(AssistantModelCall(f"o{3 + i}", "search_discovery", {"query": f"Position 2024-09-30 {acc}", "limit": 5})
                  for i, acc in enumerate(("stk-0", "stk-1", "stk-2", "stk-3"))),
            ))
        calcs = [m for m in tool_msgs if "operation" in m]
        buy = next(h for m in tool_msgs for h in m.get("hits", ()) if "event=buyback" in h["excerpt"])
        accs = [next(h for m in tool_msgs for h in m.get("hits", ()) if f"date=2024-09-30; account={acc}" in h["excerpt"])
                for acc in ("stk-0", "stk-1", "stk-2", "stk-3")]
        if not calcs:
            inputs = [{"label": f"stk-{i}", "value": re.search(r"shares=(\d+)", h["excerpt"]).group(1),
                       "source_refs": [{k: h[k] for k in ("document_id", "source_name", "location")}]} for i, h in enumerate(accs)]
            return AssistantTurn(tool_calls=(AssistantModelCall("o7", "calculate", {"operation": "sum", "inputs": inputs, "unit": "shares"}),))
        if len(calcs) == 1:
            buy_shares = re.search(r"shares=(\d+)", buy["excerpt"]).group(1)
            return AssistantTurn(tool_calls=(AssistantModelCall("o8", "calculate", {"operation": "percent", "inputs": [
                {"label": "combined", "value": calcs[0]["result"], "source_refs": [{k: h[k] for k in ("document_id", "source_name", "location")} for h in accs]},
                {"label": "outstanding", "value": buy_shares, "source_refs": [{k: buy[k] for k in ("document_id", "source_name", "location")}]},
            ]}),))
        pct = calcs[1]["result"]
        cites = [{k: h[k] for k in ("document_id", "source_name", "location")} for h in (*accs, buy)]
        return AssistantTurn(content=json.dumps({
            "answerable": True, "summary": f"Yes. Combined holdings reached {pct}% of outstanding shares.",
            "findings": [{"text": f"Combined holdings across stk-0..stk-3 reached {pct}% after the 2024-09-01 buyback.",
                          "citations": cites, "calculation_ids": ["calc-1", "calc-2"]}],
            "gaps": [], "competing_accounts": [],
        }))


def run_evaluation(client, *, seed: int = 7, tool_style: str = "openai", max_turns: int = 12) -> dict[str, object]:
    suite, backend = build_stock_eval_suite(seed=seed)
    rows = []
    for case in suite:
        runtime = MatterToolRuntime(backend.matter_id, "eval-defender", backend)
        result = CaseAssistant(client, tool_style=tool_style).investigate(case.question, runtime, max_turns=max_turns)
        rows.append(score_eval_case(case, result))
    avg = round(sum(r["score"] for r in rows) / len(rows), 2) if rows else 0.0
    return {"suite": "stock-ownership", "seed": seed, "score": avg, "cases": rows}


def render_markdown(report: Mapping[str, object]) -> str:
    lines = [
        f"case-assistant-eval {report['score']:.1f}/100 (suite={report['suite']}, seed={report['seed']})",
        "", "| Case | Score | Turns | Receipts | Penalties |", "| --- | ---: | ---: | ---: | --- |",
    ]
    for row in report["cases"]:
        pen = ", ".join(row["penalties"]) if row["penalties"] else "none"
        lines.append(f"| `{row['case_id']}` | {row['score']:.1f} | {row['turns_used']} | {row['receipts']} | {pen} |")
    return "\n".join(lines) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend", choices=("scripted", "openai", "ollama"), default="scripted")
    parser.add_argument("--url", default="")
    parser.add_argument("--model", default="")
    parser.add_argument("--api-key", default="")
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--max-turns", type=int, default=12)
    parser.add_argument("--out-json", type=Path)
    parser.add_argument("--out-md", type=Path)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)
    for out_path in (args.out_json, args.out_md):
        if out_path is not None and out_path.exists() and not args.force:
            print(f"Refusing to overwrite existing file without --force: {out_path}", file=sys.stderr)
            return 2
    if args.backend == "scripted":
        client, style = _ScriptedEvalModel(), "openai"
    else:
        if not args.url or not args.model:
            print("--url and --model are required for openai/ollama backends.", file=sys.stderr)
            return 2
        client = (OpenAIAssistantClient(args.url, args.model, api_key=args.api_key)
                  if args.backend == "openai" else OllamaAssistantClient(args.url, args.model, api_key=args.api_key))
        style = args.backend
    report = run_evaluation(client, seed=args.seed, tool_style=style, max_turns=args.max_turns)
    md = render_markdown(report)
    if args.out_json is not None:
        args.out_json.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if args.out_md is not None:
        args.out_md.write_text(md, encoding="utf-8")
    print(md, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
