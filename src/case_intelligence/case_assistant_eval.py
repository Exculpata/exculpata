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
    AssistantToolError, MatterToolRuntime, SearchHit, SearchResponse, SourcePage, SourceReadResult, SourceSummary,
)
from .exact_search import QuerySyntaxError, match_positionals, parse_query, tokenize_text
from .stock_case_gold import build_case
from .stock_case_render import documents as render_stock_documents

_UNREADABLE_SCAN_NAME = Path("damaged-scan.pdf").name


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
    expected_conclusion_pattern: str = ""
    required_gap_pattern: str = ""


@dataclass(frozen=True)
class _EvalDoc:
    summary: SourceSummary
    pages: tuple[SourcePage, ...]


class InMemoryEvalBackend:
    """Deterministic MatterToolBackend backed by a rendered synthetic discovery corpus."""

    def __init__(
        self,
        matter_id: str,
        docs: Sequence[_EvalDoc],
        *,
        email_threads: Mapping[str, Sequence[Mapping[str, object]]] = (),
        transcript_cues: Mapping[str, Sequence[Mapping[str, object]]] = (),
        production_audit: Mapping[str, object] | None = None,
    ) -> None:
        self.matter_id = matter_id
        self.docs = {d.summary.document_id: d for d in docs}
        self._email_threads = dict(email_threads)
        self._transcript_cues = dict(transcript_cues)
        self._production_audit = dict(production_audit) if production_audit is not None else None

    def authorize(self, matter_id: str, actor_id: str) -> bool:
        return matter_id == self.matter_id

    def list_sources(self, matter_id: str, *, query: str = "", kind: str = "",
                     status: str = "", offset: int = 0, limit: int = 50,
                     document_ids: frozenset[str] | None = None):
        rows = [d.summary for d in self.docs.values()
                if (document_ids is None or d.summary.document_id in document_ids)
                and (not kind or d.summary.kind == kind) and (not status or d.summary.status == status)
                and (not query or query.casefold() in (d.summary.source_name + d.summary.bates).casefold())]
        return rows[offset:offset + limit], len(rows)

    def search_discovery(self, matter_id: str, query: str, *, mode: str = "hybrid",
                         limit: int = 20, offset: int = 0, document_ids: frozenset[str] | None = None) -> SearchResponse:
        scoped_docs = [
            d for d in self.docs.values()
            if document_ids is None or d.summary.document_id in document_ids
        ]
        excluded_unavailable = sum(1 for d in scoped_docs if d.summary.status != "ready")
        ready_docs = [d for d in scoped_docs if d.summary.status == "ready"]
        hits: list[SearchHit] = []
        if mode == "exact":
            try:
                parsed = parse_query(query)
            except QuerySyntaxError as exc:
                raise AssistantToolError(f"Invalid exact query: {exc}") from exc
            for d in ready_docs:
                proof = parsed.explain_units(p.text for p in d.pages)
                if proof is None:
                    continue
                for p in d.pages:
                    tokens = tokenize_text(p.text)
                    if not tokens:
                        continue
                    if proof:
                        matched_nodes = tuple(match_positionals(tokens, proof))
                        if not matched_nodes:
                            continue
                        score = float(len(matched_nodes))
                    else:
                        score = 1.0
                    hits.append(SearchHit(
                        d.summary.document_id, d.summary.source_name, p.location, p.text,
                        score, p.source_version_id,
                    ))
        else:
            terms = [t for t in re.findall(r"[a-z0-9_-]+", query.casefold()) if len(t) > 1]
            hits = [
                SearchHit(d.summary.document_id, d.summary.source_name, p.location, p.text,
                          float(sum(1 for t in terms if t in p.text.casefold())), p.source_version_id)
                for d in ready_docs
                for p in d.pages if any(t in p.text.casefold() for t in terms)
            ]
        hits.sort(key=lambda item: (-item.score, item.document_id, item.location))
        page = hits[offset:offset + limit]
        return SearchResponse(
            hits=tuple(page),
            total_matches=len(hits),
            offset=offset,
            has_more=(offset + len(page) < len(hits)),
            excluded_unavailable_count=excluded_unavailable,
        )

    def read_source(self, matter_id: str, document_id: str, *, start_page: int = 1, end_page: int | None = None):
        doc = self.docs.get(document_id)
        if doc is None:
            return None
        stop = len(doc.pages) if end_page is None else min(end_page, len(doc.pages))
        return SourceReadResult(doc.summary.document_id, doc.summary.source_name, len(doc.pages),
                                tuple(p for p in doc.pages if start_page <= p.page_number <= stop))

    def get_case_knowledge(self, matter_id: str, *, section: str = "all", limit: int = 50,
                           document_ids: frozenset[str] | None = None) -> Mapping[str, object]:
        return {}

    def read_email_thread(
        self, matter_id: str, document_id: str, *, limit: int = 25, offset: int | None = None,
        allowed_document_ids: frozenset[str] | None = None,
    ) -> Mapping[str, object] | None:
        if matter_id != self.matter_id or document_id not in self._email_threads:
            return None
        if allowed_document_ids is not None and document_id not in allowed_document_ids:
            return None
        msgs = [
            dict(m) for m in self._email_threads[document_id]
            if allowed_document_ids is None or m["document_id"] in allowed_document_ids
        ]
        start = offset or 0
        window = msgs[start:start + limit]
        return {
            "document_id": document_id,
            "count": len(window),
            "total_messages": len(msgs),
            "messages": window,
        }

    def read_transcript_window(
        self, matter_id: str, document_id: str, *, start_seconds: float = 0.0,
        end_seconds: float | None = None, limit: int = 50, offset: int = 0,
    ) -> Mapping[str, object] | None:
        if matter_id != self.matter_id or document_id not in self._transcript_cues:
            return None
        doc = self.docs[document_id]
        cues = [
            dict(c) for c in self._transcript_cues[document_id]
            if c["start_seconds"] >= start_seconds and (end_seconds is None or c["start_seconds"] <= end_seconds)
        ]
        window = cues[offset:offset + limit]
        return {
            "document_id": document_id,
            "source_name": doc.summary.source_name,
            "count": len(window),
            "total_cues": len(cues),
            "cues": window,
        }

    def inspect_production(
        self, matter_id: str, *, load_file_document_id: str = "", include_gaps: bool = True,
        document_ids: frozenset[str] | None = None,
    ) -> Mapping[str, object]:
        scoped = [d for d in self.docs.values() if document_ids is None or d.summary.document_id in document_ids]
        unavail = [asdict_summary(d.summary) for d in scoped if d.summary.status != "ready"]
        counts: dict[str, int] = {}
        for d in scoped:
            counts[d.summary.kind] = counts.get(d.summary.kind, 0) + 1
        audit = dict(self._production_audit or {})
        if not include_gaps or (document_ids is not None and audit.get("load_file_id") not in document_ids):
            bates_gaps = {"computed": False, "notice": "Bates gaps omitted or outside scope.", "facts": [], "counts": {}}
        else:
            bates_gaps = dict(audit.get("bates_gaps") or {"computed": False, "notice": "", "facts": [], "counts": {}})
        source_refs = list(audit.get("source_refs") or ())
        if document_ids is not None:
            source_refs = [r for r in source_refs if r["document_id"] in document_ids]
        return {
            "total_sources": len(scoped),
            "recorded_pdf_pages": sum(d.summary.page_count for d in scoped if d.summary.kind == "pdf"),
            "counts_by_media_type": counts,
            "unavailable_count": len(unavail),
            "unavailable_sources": unavail[:25],
            "load_file_attempts": list(audit.get("load_file_attempts") or ()),
            "load_file_attempts_truncated": False,
            "bates_gaps": bates_gaps,
            "source_refs": source_refs,
        }


def asdict_summary(summary: SourceSummary) -> dict[str, object]:
    return {
        "document_id": summary.document_id, "source_name": summary.source_name,
        "kind": summary.kind, "status": summary.status, "bates": summary.bates,
        "status_detail": summary.status_detail,
    }


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
    docs.append(_EvalDoc(SourceSummary("stock-damaged", _UNREADABLE_SCAN_NAME, "pdf", "failed", 0, status_detail="Unreadable image stream"), ()))
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
            expected_conclusion_pattern=r"\b(?:contradict\w*|inconsistent|inconsistenc\w*|conflict\w*|discrepanc\w*|not consistent)\b",
        ),
        EvalCase(
            "unrecorded_market_price_trap",
            "What was the closing market price per share of Orbel Spindle Works on September 30, 2024?",
            expected_answerable=False,
        ),
    )
    return suite, InMemoryEvalBackend(case["matter_id"], docs)


def build_extended_eval_suite(seed: int = 7) -> tuple[tuple[EvalCase, ...], InMemoryEvalBackend]:
    """Build the 8-case extended synthesis suite exercising Bates gaps, email threads, transcripts, and CSV tables."""
    stock_suite, stock_backend = build_stock_eval_suite(seed=seed)
    docs = list(stock_backend.docs.values())

    ledger_csv = (
        "wire_id,from_account,amount,date\n"
        "w-1,stk-0,12500.50,2024-09-01\n"
        "w-2,stk-1,4000.00,2024-09-02\n"
        "w-3,stk-0,7500.25,2024-09-05\n"
    )
    docs.extend([
        _EvalDoc(
            SourceSummary("ext-bates-csv", "EXT-0001-loadfile.csv", "loadfile", "ready", 1, bates="PROD0001"),
            (SourcePage(1, "Page 1", "path,begin_bates,end_bates\nalpha.pdf,PROD0001,PROD0002\nbeta.pdf,PROD0004,PROD0004\n", "v1-ext-bates-csv"),),
        ),
        _EvalDoc(
            SourceSummary("ext-email-1", "EXT-0001-email.eml", "email", "ready", 1, bates="PROD0010"),
            (SourcePage(1, "Page 1", "Subject: Side letter draft\nInitial terms proposed for Q3 wire transfers.", "v1-ext-email-1"),),
        ),
        _EvalDoc(
            SourceSummary("ext-email-2", "EXT-0002-email.eml", "email", "ready", 1, bates="PROD0011"),
            (SourcePage(1, "Page 1", "Subject: Re: Side letter draft\nSee attached signed-side-letter.bin (attachment not extracted).", "v1-ext-email-2"),),
        ),
        _EvalDoc(
            SourceSummary("ext-email-3", "EXT-0003-email.eml", "email", "ready", 1, bates="PROD0012"),
            (SourcePage(1, "Page 1", "Subject: Re: Side letter draft\nProceeding with the stk-0 transfer schedule.", "v1-ext-email-3"),),
        ),
        _EvalDoc(
            SourceSummary("ext-transcript", "EXT-0004-transcript.txt", "transcript", "ready", 1, bates="PROD0013"),
            (SourcePage(1, "00:01:30–00:01:45", "[00:01:30] Witness A: Account stk-0 never sent any wire transfers in September 2024.", "v1-ext-transcript"),),
        ),
        _EvalDoc(
            SourceSummary("ext-ledger", "EXT-0005-ledger.csv", "ledger", "ready", 1, bates="PROD0014"),
            (SourcePage(1, "Line 1", ledger_csv, "v1-ext-ledger"),),
        ),
    ])

    thread_messages = (
        {"document_id": "ext-email-1", "source_name": "EXT-0001-email.eml", "location": "Page 1", "source_version_id": "v1-ext-email-1",
         "body": "Initial terms proposed for Q3 wire transfers.", "attachment_gaps": []},
        {"document_id": "ext-email-2", "source_name": "EXT-0002-email.eml", "location": "Page 1", "source_version_id": "v1-ext-email-2",
         "body": "See attached signed-side-letter.bin (attachment not extracted).", "attachment_gaps": ["signed-side-letter.bin"]},
        {"document_id": "ext-email-3", "source_name": "EXT-0003-email.eml", "location": "Page 1", "source_version_id": "v1-ext-email-3",
         "body": "Proceeding with the stk-0 transfer schedule.", "attachment_gaps": []},
    )
    email_threads = {m["document_id"]: thread_messages for m in thread_messages}
    transcript_cues = {
        "ext-transcript": (
            {"start_seconds": 15.0, "end_seconds": 30.0, "speaker": "Interviewer", "location": "00:00:15–00:00:30",
             "source_version_id": "v1-ext-transcript", "text": "Opening statement for the recorded interview."},
            {"start_seconds": 90.0, "end_seconds": 105.0, "speaker": "Witness A", "location": "00:01:30–00:01:45",
             "source_version_id": "v1-ext-transcript", "text": "Account stk-0 never sent any wire transfers in September 2024."},
        ),
    }
    bates_ref = {"document_id": "ext-bates-csv", "source_name": "EXT-0001-loadfile.csv", "location": "Page 1", "source_version_id": "v1-ext-bates-csv"}
    production_audit = {
        "load_file_id": "ext-bates-csv",
        "load_file_attempts": [{"document_id": "ext-bates-csv", "source_format": "csv", "status": "complete"}],
        "source_refs": [bates_ref],
        "bates_gaps": {
            "computed": True,
            "notice": "Bates gap findings reflect the inspected load file only.",
            "load_file_ref": bates_ref,
            "counts": {"missing": 1},
            "facts": [{"text": "Missing Bates PROD0003 between PROD0002 and PROD0004.", "source_refs": [bates_ref]}],
        },
    }
    extended_cases = (
        EvalCase(
            "bates_gap_production_audit",
            "Inspect the production load file for any missing Bates numbers and note unreadable files.",
            expected_answerable=True, require_receipt=True,
            required_source_kinds=("loadfile",), require_gap_notice=True,
            expected_conclusion_pattern=r"\bPROD0003\b",
        ),
        EvalCase(
            "email_thread_attachment_gap",
            "Summarize the side-letter email thread chronologically and identify any unextracted attachment gaps.",
            expected_answerable=True, required_source_kinds=("email",),
            require_gap_notice=True, required_gap_pattern=r"signed-side-letter\.bin|attachment",
            expected_conclusion_pattern=r"signed-side-letter\.bin|side letter",
        ),
        EvalCase(
            "transcript_window_contradiction",
            "Compare Witness A's recorded interview around 00:01:30 against the wire ledger for account stk-0.",
            expected_answerable=True, required_source_kinds=("transcript", "ledger"),
            require_competing_accounts=True,
            expected_conclusion_pattern=r"\b(?:contradict\w*|inconsistent|conflict\w*|discrepanc\w*)\b",
        ),
        EvalCase(
            "table_ledger_aggregation",
            "What is the exact total dollar amount wired from account stk-0 in the wire ledger?",
            expected_answerable=True, expected_value="20000.75", tolerance=0.0,
            require_receipt=True, required_source_kinds=("ledger",),
        ),
    )
    backend = InMemoryEvalBackend(
        stock_backend.matter_id, docs,
        email_threads=email_threads, transcript_cues=transcript_cues, production_audit=production_audit,
    )
    return (*stock_suite, *extended_cases), backend


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
        if result.omitted_findings > 0 or result.findings:
            return {"case_id": case.case_id, "score": 0.0, "penalties": ["trap_rejected_claim"],
                    "turns_used": result.turns_used, "receipts": len(result.receipts)}
        if any(
            token in g.casefold()
            for g in result.gaps
            for token in ("unstructured output", "invalid ", "turn budget")
        ):
            return {"case_id": case.case_id, "score": 0.0, "penalties": ["trap_unstructured_or_incomplete"],
                    "turns_used": result.turns_used, "receipts": len(result.receipts)}
        return {"case_id": case.case_id, "score": 100.0, "penalties": [],
                "turns_used": result.turns_used, "receipts": len(result.receipts)}

    accuracy, citations_pts, math_pts, honesty_pts = 0.0, 0.0, 0.0, 15.0
    answer_texts = [result.summary, *(f.text for f in result.findings)]
    if result.answerable:
        if case.expected_value is not None:
            if _matches_numeric(case.expected_value, answer_texts, case.tolerance):
                accuracy = 40.0
            else:
                penalties.append("numeric_mismatch")
        elif case.expected_conclusion_pattern:
            if any(re.search(case.expected_conclusion_pattern, text, flags=re.IGNORECASE) for text in answer_texts):
                accuracy = 40.0
            else:
                penalties.append("conclusion_mismatch")
        else:
            accuracy = 40.0
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
        used_ids = {cid for f in result.findings for cid in f.calculation_ids}
        receipt_vals = [result.receipts[cid].result for cid in used_ids if cid in result.receipts]
        if case.expected_value:
            if _matches_numeric(case.expected_value, receipt_vals, case.tolerance):
                math_pts = 20.0
            else:
                penalties.append("missing_calculation_receipt")
        elif used_ids and any(cid in result.receipts for cid in used_ids):
            math_pts = 20.0
        else:
            penalties.append("missing_calculation_receipt")
    else:
        math_pts = 20.0

    if result.omitted_findings > 0:
        honesty_pts = max(0.0, honesty_pts - 5.0 * result.omitted_findings)
        penalties.append("omitted_unverified_findings")
    if case.require_gap_notice:
        if case.required_gap_pattern:
            gap_ok = any(re.search(case.required_gap_pattern, g, flags=re.IGNORECASE) for g in result.gaps)
        else:
            gap_ok = any(_UNREADABLE_SCAN_NAME in g or "unavailable" in g.casefold() for g in result.gaps)
        if not gap_ok:
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
    """Reference synthetic model proving the stock and extended eval suites end to end."""

    def __init__(self) -> None:
        self._step = 0

    def complete_turn(self, messages, *, tools):
        q = next(m["content"] for m in messages if m["role"] == "user")
        tool_msgs = [json.loads(m["content"]) for m in messages if m.get("role") == "tool"]
        if "missing Bates numbers" in q:
            if not tool_msgs:
                return AssistantTurn(tool_calls=(AssistantModelCall("b1", "inspect_production", {"load_file_document_id": "ext-bates-csv"}),))
            payload = tool_msgs[0]
            cite = payload["bates_gaps"]["facts"][0]["source_refs"][0]
            rid = payload["calculation"]["receipt_id"]
            return AssistantTurn(content=json.dumps({
                "answerable": True,
                "summary": "Production inspection identified Missing Bates PROD0003 between PROD0002 and PROD0004.",
                "findings": [{"text": "Missing Bates PROD0003 between PROD0002 and PROD0004.",
                              "citations": [cite], "calculation_ids": [rid]}],
                "gaps": [], "competing_accounts": [],
            }))
        if "side-letter email thread" in q:
            if not tool_msgs:
                return AssistantTurn(tool_calls=(AssistantModelCall("e1", "read_email_thread", {"document_id": "ext-email-2"}),))
            msgs = tool_msgs[0]["messages"]
            cites = [{k: m[k] for k in ("document_id", "source_name", "location", "source_version_id")} for m in msgs]
            return AssistantTurn(content=json.dumps({
                "answerable": True,
                "summary": "The side-letter thread proposed Q3 terms, referenced unextracted attachment signed-side-letter.bin, and proceeded with the stk-0 transfer schedule.",
                "findings": [{"text": "The side letter thread referenced signed-side-letter.bin (attachment not extracted) before proceeding with stk-0 transfers.",
                              "citations": cites, "calculation_ids": []}],
                "gaps": ["Attachment signed-side-letter.bin was not extracted and could not be reviewed."],
                "competing_accounts": [],
            }))
        if "recorded interview around 00:01:30" in q:
            if not tool_msgs:
                return AssistantTurn(tool_calls=(
                    AssistantModelCall("tw1", "read_transcript_window", {"document_id": "ext-transcript", "start_seconds": 60, "end_seconds": 120}),
                    AssistantModelCall("tw2", "query_table", {"document_id": "ext-ledger", "filters": {"from_account": "stk-0"}}),
                ))
            tr = next(m for m in tool_msgs if "cues" in m)
            tb = next(m for m in tool_msgs if "rows" in m)
            cue = tr["cues"][0]
            tr_cite = {"document_id": tr["document_id"], "source_name": tr["source_name"], "location": cue["location"], "source_version_id": cue["source_version_id"]}
            tb_cite = {"document_id": tb["document_id"], "source_name": tb["source_name"], "location": tb["location"], "source_version_id": tb["source_version_id"]}
            return AssistantTurn(content=json.dumps({
                "answerable": True,
                "summary": "Witness A's recorded interview contradicts the wire ledger for account stk-0.",
                "findings": [{"text": "Witness A denied stk-0 sent wires in September 2024, which contradicts wire ledger entries w-1 and w-3.",
                              "citations": [tr_cite, tb_cite], "calculation_ids": []}],
                "gaps": [],
                "competing_accounts": ["Transcript denies any September 2024 stk-0 wires while the CSV ledger records two stk-0 transfers."],
            }))
        if "exact total dollar amount wired from account stk-0" in q:
            if not tool_msgs:
                return AssistantTurn(tool_calls=(AssistantModelCall("qt1", "query_table", {
                    "document_id": "ext-ledger", "filters": {"from_account": "stk-0"},
                    "aggregate_column": "amount", "aggregate_op": "sum",
                }),))
            tb = tool_msgs[0]
            val = tb["calculation"]["result"]
            rid = tb["calculation"]["receipt_id"]
            tb_cite = {"document_id": tb["document_id"], "source_name": tb["source_name"], "location": tb["location"], "source_version_id": tb["source_version_id"]}
            return AssistantTurn(content=json.dumps({
                "answerable": True,
                "summary": f"Account stk-0 wired a total of {val}.",
                "findings": [{"text": f"Wire ledger entries for account stk-0 total {val}.",
                              "citations": [tb_cite], "calculation_ids": [rid]}],
                "gaps": [], "competing_accounts": [],
            }))
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
                    {"label": "buyback", "value": "2024-09-01", "source_refs": [{k: buy[k] for k in ("document_id", "source_name", "location", "source_version_id")}]},
                    {"label": "sale", "value": "2024-10-15", "source_refs": [{k: sale[k] for k in ("document_id", "source_name", "location", "source_version_id")}]},
                ]}),))
            buy = next(h for m in tool_msgs for h in m.get("hits", ()) if "-filing." in h["source_name"])
            sale = next(h for m in tool_msgs for h in m.get("hits", ()) if "-confirmation." in h["source_name"])
            return AssistantTurn(content=json.dumps({
                "answerable": True, "summary": "44 calendar days elapsed between 2024-09-01 and 2024-10-15.",
                "findings": [{"text": "44 days elapsed between the 2024-09-01 buyback and the 2024-10-15 sales.",
                              "citations": [{k: buy[k] for k in ("document_id", "source_name", "location", "source_version_id")},
                                            {k: sale[k] for k in ("document_id", "source_name", "location", "source_version_id")}],
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
                "findings": [{"text": "The associate's interview ('I never received trading instructions') contradicts emails sending monthly share orders for stk-4.",
                              "citations": [{k: iv[k] for k in ("document_id", "source_name", "location", "source_version_id")},
                                            {k: em[k] for k in ("document_id", "source_name", "location", "source_version_id")}],
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
                       "source_refs": [{k: h[k] for k in ("document_id", "source_name", "location", "source_version_id")}]} for i, h in enumerate(accs)]
            return AssistantTurn(tool_calls=(AssistantModelCall("o7", "calculate", {"operation": "sum", "inputs": inputs, "unit": "shares"}),))
        if len(calcs) == 1:
            buy_shares = re.search(r"shares=(\d+)", buy["excerpt"]).group(1)
            return AssistantTurn(tool_calls=(AssistantModelCall("o8", "calculate", {"operation": "percent", "inputs": [
                {"label": "combined", "value": calcs[0]["result"], "source_refs": [{k: h[k] for k in ("document_id", "source_name", "location", "source_version_id")} for h in accs]},
                {"label": "outstanding", "value": buy_shares, "source_refs": [{k: buy[k] for k in ("document_id", "source_name", "location", "source_version_id")}]},
            ]}),))
        pct = calcs[1]["result"]
        cites = [{k: h[k] for k in ("document_id", "source_name", "location", "source_version_id")} for h in (*accs, buy)]
        return AssistantTurn(content=json.dumps({
            "answerable": True, "summary": f"Yes. Combined holdings reached {pct}% of outstanding shares.",
            "findings": [{"text": f"Combined holdings across stk-0..stk-3 reached {pct}% after the 2024-09-01 buyback.",
                          "citations": cites, "calculation_ids": ["calc-1", "calc-2"]}],
            "gaps": [], "competing_accounts": [],
        }))


def run_evaluation(
    client,
    *,
    seed: int = 7,
    tool_style: str = "openai",
    max_turns: int = 12,
    backend: str | None = None,
    model: str | None = None,
    suite: str = "stock-ownership",
) -> dict[str, object]:
    if suite == "extended":
        cases, backend_impl = build_extended_eval_suite(seed=seed)
    elif suite == "stock-ownership":
        cases, backend_impl = build_stock_eval_suite(seed=seed)
    else:
        raise ValueError(f"Unsupported eval suite: {suite!r}")
    rows = []
    for case in cases:
        runtime = MatterToolRuntime(backend_impl.matter_id, "eval-defender", backend_impl)
        result = CaseAssistant(client, tool_style=tool_style).investigate(case.question, runtime, max_turns=max_turns)
        rows.append(score_eval_case(case, result))
    avg = round(sum(r["score"] for r in rows) / len(rows), 2) if rows else 0.0
    resolved_backend = backend or ("scripted" if isinstance(client, _ScriptedEvalModel) else tool_style)
    synthetic_reference = resolved_backend == "scripted"
    resolved_model = model or ("scripted-reference" if synthetic_reference else (getattr(client, "model", "") or "unknown"))
    return {
        "suite": suite,
        "seed": seed,
        "backend": resolved_backend,
        "model": resolved_model,
        "max_turns": max_turns,
        "synthetic_reference": synthetic_reference,
        "score": avg,
        "cases": rows,
    }


def render_markdown(report: Mapping[str, object]) -> str:
    backend = report.get("backend", "unknown")
    model = report.get("model", "unknown")
    max_turns = report.get("max_turns", "unknown")
    lines = [
        (
            f"case-assistant-eval {report['score']:.1f}/100 "
            f"(suite={report['suite']}, seed={report['seed']}, backend={backend}, "
            f"model={model}, max_turns={max_turns})"
        ),
    ]
    if report.get("synthetic_reference"):
        lines.append(
            "> Synthetic reference run (`backend=scripted`): harness verification only, not live-model evidence."
        )
    lines.extend(["", "| Case | Score | Turns | Receipts | Penalties |", "| --- | ---: | ---: | ---: | --- |"])
    for row in report["cases"]:
        pen = ", ".join(row["penalties"]) if row["penalties"] else "none"
        lines.append(f"| `{row['case_id']}` | {row['score']:.1f} | {row['turns_used']} | {row['receipts']} | {pen} |")
    return "\n".join(lines) + "\n"


def _write_output_file(path: Path, content: str, *, force: bool) -> None:
    mode = "w" if force else "x"
    with path.open(mode, encoding="utf-8") as handle:
        handle.write(content)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend", choices=("scripted", "openai", "ollama"), default="scripted")
    parser.add_argument("--suite", choices=("stock-ownership", "extended"), default="stock-ownership")
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
        client, style, model_name = _ScriptedEvalModel(), "openai", "scripted-reference"
    else:
        if not args.url or not args.model:
            print("--url and --model are required for openai/ollama backends.", file=sys.stderr)
            return 2
        client = (OpenAIAssistantClient(args.url, args.model, api_key=args.api_key)
                  if args.backend == "openai" else OllamaAssistantClient(args.url, args.model, api_key=args.api_key))
        style, model_name = args.backend, args.model
    report = run_evaluation(
        client,
        seed=args.seed,
        tool_style=style,
        max_turns=args.max_turns,
        backend=args.backend,
        model=model_name,
        suite=args.suite,
    )
    md = render_markdown(report)
    try:
        if args.out_json is not None:
            _write_output_file(args.out_json, json.dumps(report, indent=2, sort_keys=True) + "\n", force=args.force)
        if args.out_md is not None:
            _write_output_file(args.out_md, md, force=args.force)
    except FileExistsError as exc:
        print(f"Refusing to overwrite existing file without --force: {exc.filename}", file=sys.stderr)
        return 2
    print(md, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
