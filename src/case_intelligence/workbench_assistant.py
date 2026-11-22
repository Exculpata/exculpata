"""App-shell adapter wiring CaseAssistant and MatterToolRuntime into WorkbenchService."""
from __future__ import annotations

from dataclasses import asdict
import os
from typing import Callable, Mapping, Sequence

from .answer_jobs import AnswerJobFailure, AnswerResult
from .case_assistant import (
    AssistantInvestigationResult, CaseAssistant, OllamaAssistantClient, OpenAIAssistantClient,
)
from .case_assistant_tools import (
    AssistantAccessError, AssistantToolError, MatterToolRuntime, SearchHit, SearchResponse, SourcePage, SourceReadResult, SourceSummary,
)
from .exact_search import match_positionals, tokenize_text
from .exact_search_results import ExactSearchUnavailable
from .generation import OllamaGenerator, OpenAICompatibleGenerator
from .generation_errors import GenerationUnavailable
from .generation_matter import generation_matter_scope
from .matter_knowledge import MatterKnowledgeService
from .review_bench_v2 import Candidate

_TRUTHY = frozenset({"1", "true", "yes", "on"})
_VERIFIED_INTRO = "Verified findings from inspected discovery:"
_ABSTENTION_INTRO = "The inspected sources did not support a verified finding for that question."


def case_assistant_enabled() -> bool:
    return os.getenv("CASE_INTELLIGENCE_CASE_ASSISTANT", "").strip().casefold() in _TRUTHY


def assistant_client_from_generator(client: object) -> tuple[object, str]:
    """Build an AssistantModelClient from the configured generator without extra config."""
    if callable(getattr(client, "complete_turn", None)):
        return client, "openai"
    if isinstance(client, OpenAICompatibleGenerator):
        auth = client._headers.get("Authorization", "")
        key = auth.removeprefix("Bearer ").strip() if auth.startswith("Bearer ") else ""
        return OpenAIAssistantClient(client.endpoint, client.model, timeout=client.timeout, api_key=key, opener=client._opener), "openai"
    if isinstance(client, OllamaGenerator):
        auth = client._headers.get("Authorization", "")
        key = auth.removeprefix("Bearer ").strip() if auth.startswith("Bearer ") else ""
        return OllamaAssistantClient(client.endpoint, client.model, timeout=client.timeout, api_key=key, opener=client._opener), "ollama"
    raise GenerationUnavailable("Configured answer backend does not support the Case Assistant tool loop.")


class WorkbenchToolBackend:
    """Authorized MatterToolBackend bound to a live WorkbenchService instance."""

    def __init__(self, bench, actor_id: str, *, allowed_notebook_items: Sequence[object] | None = None) -> None:
        self.bench = bench
        self.actor_id = actor_id
        self.allowed_notebook_items = None if allowed_notebook_items is None else tuple(allowed_notebook_items)
        self.citations_by_key: dict[tuple[str, str], object] = {}
        self.citations_by_identity: dict[tuple[str, str, str, str], object] = {}

    def _remember_citation(self, wc) -> None:
        self.citations_by_key[(wc.document_id, wc.location)] = wc
        identity = (wc.document_id, wc.location, wc.source_version_id, wc.excerpt_digest)
        self.citations_by_identity.setdefault(identity, wc)

    def citation_for_ref(self, ref) -> object | None:
        """Resolve a verified SourceRef only against its exact inspected source identity."""
        doc_id = str(getattr(ref, "document_id", "") or "")
        location = str(getattr(ref, "location", "") or "")
        version_id = str(getattr(ref, "source_version_id", "") or "")
        digest = str(getattr(ref, "excerpt_digest", "") or "")
        if digest:
            return self.citations_by_identity.get((doc_id, location, version_id, digest))
        matches = [
            wc for (d, loc, ver, _dig), wc in self.citations_by_identity.items()
            if d == doc_id and loc == location and (not version_id or ver == version_id)
        ]
        return matches[0] if len(matches) == 1 else None

    def authorize(self, matter_id: str, actor_id: str) -> bool:
        try:
            self.bench.workspace.membership(matter_id, actor_id)
            return True
        except KeyError:
            return False

    def _unit_citation(self, matter, store, doc, ordinal: int, unit):
        candidate = Candidate(
            matter_id=matter.matter_id, document_id=doc.document_id, chunk_id=f"chunk-{ordinal}",
            source_name=doc.display_name, page_number=unit.number, text=unit.text, score=1.0,
            line_start=unit.line_start, line_end=unit.line_end, source_version_id=doc.version_id,
            excerpt_digest=unit.excerpt_digest, location_label=unit.location_label or None,
        )
        return self.bench._citation(matter, candidate, store=store)

    def list_sources(
        self, matter_id: str, *, query: str = "", kind: str = "", status: str = "",
        offset: int = 0, limit: int = 50, document_ids: frozenset[str] | None = None,
    ) -> tuple[Sequence[SourceSummary], int]:
        matter = self.bench._matter_by_id(matter_id)
        store = self.bench.source_store(matter)
        with store._lock:
            docs = list(store.documents.values())
        rows: list[SourceSummary] = []
        needle = query.strip().casefold()
        for doc in docs:
            if document_ids is not None and doc.document_id not in document_ids:
                continue
            norm_status = "ready" if doc.state == "ready" else "failed"
            doc_kind = doc.media_type.split("/")[-1] if "/" in doc.media_type else (doc.media_type or "document")
            if status and norm_status != status:
                continue
            if kind and kind.casefold() not in (doc_kind.casefold(), doc.media_type.casefold()):
                continue
            if needle and needle not in (doc.display_name + " " + doc.relative_path).casefold():
                continue
            rows.append(SourceSummary(
                document_id=doc.document_id, source_name=doc.display_name, kind=doc_kind,
                status=norm_status, page_count=doc.page_count or doc.total_units,
                path=doc.relative_path, status_detail="" if norm_status == "ready" else (doc.message or doc.state),
            ))
        rows.sort(key=lambda item: (item.source_name.casefold(), item.document_id))
        return rows[offset:offset + limit], len(rows)

    def search_discovery(
        self, matter_id: str, query: str, *, mode: str = "hybrid",
        limit: int = 20, offset: int = 0, document_ids: frozenset[str] | None = None,
    ) -> SearchResponse:
        matter = self.bench._matter_by_id(matter_id)
        if mode == "exact":
            store = self.bench.source_store(matter)
            all_hits: list[SearchHit] = []
            excluded_unavail = 0
            omitted_preview = 0
            try:
                with store._lock:
                    store._ensure_active()
                    scoped_docs = [d for d in store.documents.values() if document_ids is None or d.document_id in document_ids]
                    page_num = 1
                    total_pages = 1
                    while page_num <= total_pages:
                        page = self.bench.exact_search_backend.search(
                            scoped_docs, query, scope=(matter.matter_id, "assistant", str(len(scoped_docs))),
                            page=page_num, page_size=50,
                        )
                        if page_num == 1:
                            excluded_unavail = sum(page.exclusions.values())
                            total_pages = max(1, page.pages)
                        for item in page.items:
                            try:
                                doc = store.get(item.document_id)
                            except KeyError:
                                continue
                            all_units = list(enumerate(doc.iter_parsed_units(), 1))
                            unit_ordinals = {
                                (u.number, u.line_start, u.line_end, u.excerpt_digest): idx
                                for idx, u in all_units
                            }
                            if item.matching_unit_count > len(item.passages):
                                proof = item.explanation.explain_units(u.text for _, u in all_units)
                                if proof is not None:
                                    matched_pairs = [
                                        (idx, u) for idx, u in all_units
                                        if (
                                            any(match_positionals(tokenize_text(u.text), proof))
                                            if proof
                                            else bool(tokenize_text(u.text))
                                        )
                                    ]
                                else:
                                    matched_pairs = []
                                if not matched_pairs:
                                    matched_pairs = [
                                        (unit_ordinals.get((u.number, u.line_start, u.line_end, u.excerpt_digest), 1), u)
                                        for u in item.passages
                                    ]
                            elif item.passages:
                                matched_pairs = [
                                    (unit_ordinals.get((u.number, u.line_start, u.line_end, u.excerpt_digest), 1), u)
                                    for u in item.passages
                                ]
                            else:
                                first_unit = next(((idx, u) for idx, u in all_units if u.text.strip()), None)
                                matched_pairs = [first_unit] if first_unit is not None else []
                            omitted_preview += max(0, item.matching_unit_count - len(matched_pairs))
                            for ordinal, unit in matched_pairs:
                                wc = self._unit_citation(matter, store, doc, ordinal, unit)
                                self._remember_citation(wc)
                                all_hits.append(SearchHit(
                                    wc.document_id, wc.source_name, wc.location, wc.excerpt, 1.0,
                                    wc.source_version_id, wc.excerpt_digest, wc.evidence_kind,
                                ))
                        page_num += 1
            except (ValueError, ExactSearchUnavailable) as exc:
                raise AssistantToolError(f"Exact search unavailable: {exc}") from exc
            sliced = tuple(all_hits[offset:offset + limit])
            total_matches = len(all_hits) + omitted_preview
            has_more = (offset + len(sliced) < len(all_hits)) or (omitted_preview > 0)
            return SearchResponse(
                hits=sliced, total_matches=total_matches, offset=offset,
                has_more=has_more, excluded_unavailable_count=excluded_unavail,
            )
        if offset >= 20:
            raise AssistantToolError("Hybrid search returns at most the top 20 ranked passages; use mode='exact' for paginated exhaustive search.")
        fetch_limit = min(offset + limit + 1, 20)
        citations = self.bench.search(matter, query, limit=fetch_limit, document_ids=document_ids)
        result: list[SearchHit] = []
        for wc in citations:
            self._remember_citation(wc)
            result.append(SearchHit(wc.document_id, wc.source_name, wc.location, wc.excerpt, 1.0,
                                    wc.source_version_id, wc.excerpt_digest, wc.evidence_kind))
        sliced = tuple(result[offset:offset + limit])
        hit_hybrid_cap = len(result) == 20 and (offset + len(sliced)) >= 20
        has_more = (offset + len(sliced) < len(result)) or hit_hybrid_cap
        return SearchResponse(hits=sliced, total_matches=len(result), offset=offset, has_more=has_more)

    def read_source(
        self, matter_id: str, document_id: str, *, start_page: int = 1, end_page: int | None = None,
    ) -> SourceReadResult | None:
        matter = self.bench._matter_by_id(matter_id)
        store = self.bench.source_store(matter)
        with store._lock:
            try:
                doc = store.get(document_id)
            except KeyError:
                return None
            all_units = list(enumerate(doc.iter_parsed_units(), 1))
            is_pdf = doc.media_type == "application/pdf" or bool(doc.page_count)
            total_pages = max(doc.page_count or 0, max((u.number for _, u in all_units), default=0)) if is_pdf else len(all_units)
            stop = total_pages if end_page is None else min(end_page, total_pages)
            pages: list[SourcePage] = []
            for ordinal, unit in all_units:
                page_key = unit.number if is_pdf else ordinal
                if page_key < start_page or page_key > stop:
                    continue
                wc = self._unit_citation(matter, store, doc, ordinal, unit)
                self._remember_citation(wc)
                pages.append(SourcePage(page_key, wc.location, unit.text, doc.version_id, unit.excerpt_digest))
            return SourceReadResult(doc.document_id, doc.display_name, total_pages, tuple(pages))

    def _paginate_knowledge_bucket(
        self, service: MatterKnowledgeService, matter_id: str, bucket: str, *, offset: int, limit: int,
    ) -> tuple[list[Mapping[str, object]], int]:
        """Traverse MatterKnowledgeService pages until offset + limit items are collected."""
        target = offset + limit
        collected: list[Mapping[str, object]] = []
        total = 0
        page_num = 1
        total_pages = 1
        while page_num <= total_pages and len(collected) < target:
            kwargs = {"entity_page": page_num, "assertion_page": 1} if bucket == "entities" else {
                "entity_page": 1, "assertion_page": page_num,
            }
            page = service.page(matter_id, self.actor_id, **kwargs)
            part = page.get(bucket) or {}
            items = list(part.get("items") or ())
            if page_num == 1:
                total = int(part.get("total", len(items)))
                total_pages = max(1, int(part.get("pages", 1)))
            if not items:
                break
            collected.extend(items)
            page_num += 1
        return collected[offset:offset + limit], max(total, len(collected))

    def get_case_knowledge(
        self, matter_id: str, *, section: str = "all", limit: int = 50, offset: int = 0,
        document_ids: frozenset[str] | None = None,
    ) -> Mapping[str, object]:
        matter = self.bench._matter_by_id(matter_id)
        output: dict[str, object] = {"offset": offset, "limit": limit}
        any_has_more = False
        if section in {"all", "notes"}:
            if self.allowed_notebook_items is not None:
                raw_notes = list(self.allowed_notebook_items)
                filtered_notes = []
                for n in raw_notes:
                    ref_ids = sorted({r.document_id for r in getattr(n, "citations", ()) if getattr(r, "document_id", "")})
                    if document_ids is not None and (not ref_ids or not set(ref_ids).issubset(document_ids)):
                        continue
                    filtered_notes.append((n, ref_ids))
                notes_total = len(filtered_notes)
                page_notes = filtered_notes[offset:offset + limit]
                notes_omitted = max(0, notes_total - (offset + len(page_notes)))
                notes_has_more = notes_omitted > 0
            else:
                fetch_cap = offset + limit + 1
                raw_notes = list(self.bench.workspace.all_notebook_items(
                    matter_id, self.actor_id, include_dismissed=False, limit=fetch_cap,
                ))
                filtered_notes = []
                for n in raw_notes:
                    ref_ids = sorted({r.document_id for r in getattr(n, "citations", ()) if getattr(r, "document_id", "")})
                    if document_ids is not None and (not ref_ids or not set(ref_ids).issubset(document_ids)):
                        continue
                    filtered_notes.append((n, ref_ids))
                notes_has_more = len(filtered_notes) > offset + limit
                page_notes = filtered_notes[offset:offset + limit]
                notes_total = len(filtered_notes) if not notes_has_more else offset + len(page_notes) + 1
                notes_omitted = max(0, notes_total - (offset + len(page_notes)))
            note_rows: list[dict[str, object]] = []
            for n, ref_ids in page_notes:
                row: dict[str, object] = {
                    "title": getattr(n, "title", ""),
                    "body": str(getattr(n, "body", ""))[:600],
                    "status": getattr(n, "status", "confirmed"),
                    "date_label": getattr(n, "date_label", ""),
                }
                if ref_ids:
                    row["source_document_ids"] = ref_ids
                note_rows.append(row)
            output["notes"] = note_rows
            output["notes_total"] = notes_total
            output["notes_omitted"] = notes_omitted
            output["notes_has_more"] = notes_has_more
            any_has_more = any_has_more or notes_has_more
        if section in {"all", "entities", "assertions"}:
            if document_ids is not None:
                if section in {"all", "entities"}:
                    output["entities"] = []
                    output["entities_total"] = 0
                    output["entities_omitted"] = 0
                    output["entities_has_more"] = False
                if section in {"all", "assertions"}:
                    output["assertions"] = []
                    output["assertions_total"] = 0
                    output["assertions_omitted"] = 0
                    output["assertions_has_more"] = False
                output["has_more"] = any_has_more
                output["next_offset"] = (offset + limit) if any_has_more else None
                return output
            try:
                service = MatterKnowledgeService(self.bench.assertion_service(matter))
            except Exception:
                service = None
            if section in {"all", "entities"}:
                ent_items, ent_total = (
                    self._paginate_knowledge_bucket(service, matter_id, "entities", offset=offset, limit=limit)
                    if service is not None else ([], 0)
                )
                output["entities"] = [
                    {"name": e.get("display_name") or e.get("canonical_name"), "type": e.get("entity_type"), "status": e.get("status")}
                    for e in ent_items
                ]
                ent_omitted = max(0, ent_total - (offset + len(ent_items)))
                ent_has_more = ent_omitted > 0
                output["entities_total"] = ent_total
                output["entities_omitted"] = ent_omitted
                output["entities_has_more"] = ent_has_more
                any_has_more = any_has_more or ent_has_more
            if section in {"all", "assertions"}:
                asn_items, asn_total = (
                    self._paginate_knowledge_bucket(service, matter_id, "assertions", offset=offset, limit=limit)
                    if service is not None else ([], 0)
                )
                output["assertions"] = [
                    {
                        "title": a.get("title"),
                        "summary": a.get("statement") or a.get("summary"),
                        "date": a.get("raw_date") or a.get("date_label"),
                        "status": a.get("status"),
                    }
                    for a in asn_items
                ]
                asn_omitted = max(0, asn_total - (offset + len(asn_items)))
                asn_has_more = asn_omitted > 0
                output["assertions_total"] = asn_total
                output["assertions_omitted"] = asn_omitted
                output["assertions_has_more"] = asn_has_more
                any_has_more = any_has_more or asn_has_more
        output["has_more"] = any_has_more
        output["next_offset"] = (offset + limit) if any_has_more else None
        return output


def _verified_receipt_expression(receipt) -> str:
    """Format verified calculation arithmetic from operation, verified numeric inputs, and result only."""
    values = ", ".join(str(inp.value) for inp in receipt.inputs)
    return f"{receipt.operation}({values}) = {receipt.result}"


def format_assistant_qualifications(payload: Mapping[str, object]) -> str:
    """Render saved Case Assistant gaps, competing accounts, and calculation receipts for reports."""
    ca = payload.get("case_assistant")
    if not isinstance(ca, Mapping):
        return ""
    sections: list[str] = []
    gaps = [str(g).strip() for g in (ca.get("gaps") or ()) if isinstance(g, str) and g.strip()]
    if gaps:
        sections.append("Investigation gaps:\n" + "\n".join(f"- {g}" for g in gaps))
    competing = [str(c).strip() for c in (ca.get("competing_accounts") or ()) if isinstance(c, str) and c.strip()]
    if competing:
        sections.append("Competing accounts:\n" + "\n".join(f"- {c}" for c in competing))
    receipts = [r for r in (ca.get("receipts") or ()) if isinstance(r, Mapping)]
    if receipts:
        lines = []
        for r in receipts:
            rid = str(r.get("receipt_id", "")).strip()
            expr = str(r.get("expression", "")).strip()
            res = str(r.get("result", "")).strip()
            unit = str(r.get("unit", "")).strip()
            label = f"[{rid}] " if rid else ""
            suffix = f" {unit}" if unit else ""
            rendered = expr if (not res or expr.endswith(f"= {res}")) else f"{expr} = {res}"
            lines.append(f"- {label}{rendered}{suffix}".strip())
        if lines:
            sections.append("Calculation receipts:\n" + "\n".join(lines))
    return "\n\n".join(sections)


def _collect_calc_premise_refs(calc_ids: Sequence[str], receipts: Mapping[str, object]) -> list[object]:
    """Collect all SourceRefs from referenced receipts and any chained upstream receipts."""
    from .case_assistant import _norm_num

    refs: list[object] = []
    seen_receipts: set[str] = set()
    by_result: dict[str, list[str]] = {}
    for rid, rec in receipts.items():
        by_result.setdefault(_norm_num(rec.result), []).append(rid)
    queue = list(calc_ids)
    while queue:
        cid = queue.pop(0)
        if cid in seen_receipts:
            continue
        rec = receipts.get(cid)
        if rec is None:
            continue
        seen_receipts.add(cid)
        for inp in rec.inputs:
            refs.extend(inp.source_refs)
            for upstream_id in by_result.get(_norm_num(inp.value), ()):
                if upstream_id not in seen_receipts:
                    queue.append(upstream_id)
    return refs


_DETERMINISTIC_OP_UNITS = {"percent": "%", "days_between": "days", "count": "rows"}


def _verified_receipt_unit(receipt, premise_wcs: Sequence[object]) -> str:
    """Retain only operation-derived or source-grounded unit tokens for verified display."""
    import re
    raw_unit = str(getattr(receipt, "unit", "") or "").strip()
    if not raw_unit:
        return ""
    if _DETERMINISTIC_OP_UNITS.get(receipt.operation) == raw_unit:
        return raw_unit
    if not re.fullmatch(r"[A-Za-z%$]{1,16}", raw_unit):
        return ""
    premise_text = "\n".join(str(getattr(wc, "excerpt", "") or "") for wc in premise_wcs)
    return raw_unit if raw_unit.casefold() in premise_text.casefold() else ""


def _result_to_answer(bench, result: AssistantInvestigationResult, backend: WorkbenchToolBackend, source_fingerprint: str) -> AnswerResult:
    claims_payload: list[dict[str, object]] = []
    retained_findings: list[object] = []
    used_citations: list[object] = []
    seen_tokens: set[str] = set()
    extra_omitted = 0

    for finding in result.findings:
        combined_refs = (*finding.citations, *_collect_calc_premise_refs(finding.calculation_ids, result.receipts))
        resolved_wcs = [backend.citation_for_ref(ref) for ref in combined_refs]
        if not resolved_wcs or any(wc is None for wc in resolved_wcs):
            extra_omitted += 1
            continue
        cite_rows: list[dict[str, object]] = []
        seen_claim_tokens: set[str] = set()
        for wc in resolved_wcs:
            assert wc is not None
            if wc.support_token not in seen_tokens:
                seen_tokens.add(wc.support_token)
                used_citations.append(wc)
            if wc.support_token in seen_claim_tokens:
                continue
            seen_claim_tokens.add(wc.support_token)
            cite_rows.append(bench._saved_answer_citation_payload(wc))
        if not cite_rows:
            extra_omitted += 1
            continue
        retained_findings.append(finding)
        claims_payload.append({"text": finding.text, "citations": cite_rows, "calculation_ids": list(finding.calculation_ids)})
    trace_rows = [{"call_id": rec.call_id, "tool_name": rec.tool_name, "status": rec.status,
                   "elapsed_ms": rec.elapsed_ms, "cited_count": len(rec.cited_refs), "error": rec.error}
                  for rec in result.trace]
    receipt_rows: list[dict[str, object]] = []
    for receipt in result.receipts.values():
        premise_refs = _collect_calc_premise_refs((receipt.receipt_id,), result.receipts)
        premise_wcs = [backend.citation_for_ref(ref) for ref in premise_refs]
        if not premise_wcs or any(wc is None for wc in premise_wcs):
            continue
        for wc in premise_wcs:
            assert wc is not None
            if wc.support_token not in seen_tokens:
                seen_tokens.add(wc.support_token)
                used_citations.append(wc)
        receipt_rows.append({
            "receipt_id": receipt.receipt_id,
            "operation": receipt.operation,
            "expression": _verified_receipt_expression(receipt),
            "result": receipt.result,
            "unit": _verified_receipt_unit(receipt, premise_wcs),
            "inputs": [
                {"value": inp.value, "source_refs": [asdict(sref) for sref in inp.source_refs]}
                for inp in receipt.inputs
            ],
        })
    supported = bool(result.answerable and claims_payload)
    intro = _VERIFIED_INTRO if supported else _ABSTENTION_INTRO
    payload: dict[str, object] = {
        "kind": "generated" if supported else "not-supported",
        "answerable": supported,
        "model_called": True,
        "introduction": intro,
        "claims": claims_payload,
        "limitation": None,
        "missing_information": "" if supported else _ABSTENTION_INTRO,
        "case_assistant": {
            "receipts": receipt_rows, "gaps": list(result.gaps),
            "competing_accounts": list(result.competing_accounts),
            "trace": trace_rows, "turns_used": result.turns_used,
            "omitted_findings": result.omitted_findings + extra_omitted,
        },
    }
    text_parts = [f.text for f in retained_findings] if supported else [_ABSTENTION_INTRO]
    return AnswerResult("\n".join(p for p in text_parts if p), payload, tuple(used_citations),
                        retrieval_source_fingerprint=source_fingerprint)


def run_workbench_assistant_job(
    bench, job, matter, *, prior: Sequence[object], scoped_document_ids: frozenset[str] | None,
    retrieval_boundary: Mapping[str, str], report_stage: Callable[[str, str], None],
    cancelled: Callable[[], bool], notebook_items: Sequence[object] | None = None,
) -> AnswerResult:
    """Execute one AnswerJobRecord through the tool-using CaseAssistant."""
    client, tool_style = assistant_client_from_generator(getattr(bench.generator, "client", None))
    backend = WorkbenchToolBackend(bench, job.actor_id, allowed_notebook_items=notebook_items)
    runtime = MatterToolRuntime(matter.matter_id, job.actor_id, backend, allowed_document_ids=scoped_document_ids)
    history = tuple((m.role, m.content) for m in prior[-8:])
    fingerprint = retrieval_boundary.get("source_fingerprint") or bench.workspace.source_availability_fingerprint(
        matter.matter_id, retrieval_boundary.get("source_set_id") or None,
    )

    def stage_bridge(stage_key: str, message: str) -> None:
        persisted = "verifying" if stage_key == "verifying" else "generating"
        report_stage(persisted, message)

    try:
        with generation_matter_scope(bench.workspace, job.matter_id):
            result = CaseAssistant(client, tool_style=tool_style).investigate(
                job.question, runtime, history=history, stage_callback=stage_bridge, cancelled=cancelled,
            )
    except AssistantAccessError as exc:
        raise AnswerJobFailure("Access to this matter was removed before the answer completed.") from exc
    if not backend.authorize(job.matter_id, job.actor_id):
        raise AnswerJobFailure("Access to this matter was removed before the answer completed.")
    return _result_to_answer(bench, result, backend, fingerprint)
