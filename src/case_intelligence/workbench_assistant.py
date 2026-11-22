"""App-shell adapter wiring CaseAssistant and MatterToolRuntime into WorkbenchService."""
from __future__ import annotations

from dataclasses import asdict
import os
from typing import Callable, Mapping, Sequence

from fastapi import Depends, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse

from .answer_jobs import AnswerJobFailure, AnswerResult
from .case_assistant import (
    AssistantInvestigationResult, CaseAssistant, OllamaAssistantClient, OpenAIAssistantClient,
)
from .case_assistant_tools import (
    AssistantAccessError, MatterToolRuntime, SearchHit, SearchResponse, SourcePage, SourceReadResult, SourceSummary,
)
from .exact_search_results import ExactSearchUnavailable
from .generation import OllamaGenerator, OpenAICompatibleGenerator
from .generation_errors import GenerationUnavailable
from .generation_matter import generation_matter_scope
from .matter_knowledge import MatterKnowledgeService
from .review_bench_v2 import Candidate
from .store_records import WorkspaceProblem

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
                            unit_ordinals = {
                                (u.number, u.line_start, u.line_end, u.excerpt_digest): idx
                                for idx, u in enumerate(doc.iter_parsed_units(), 1)
                            }
                            for unit in item.passages:
                                ordinal = unit_ordinals.get((unit.number, unit.line_start, unit.line_end, unit.excerpt_digest), 1)
                                wc = self._unit_citation(matter, store, doc, ordinal, unit)
                                self.citations_by_key[(wc.document_id, wc.location)] = wc
                                all_hits.append(SearchHit(
                                    wc.document_id, wc.source_name, wc.location, wc.excerpt, 1.0,
                                    wc.source_version_id, wc.excerpt_digest, wc.evidence_kind,
                                ))
                        page_num += 1
            except (ValueError, ExactSearchUnavailable):
                return SearchResponse(hits=(), total_matches=0, offset=offset, has_more=False, excluded_unavailable_count=0)
            sliced = tuple(all_hits[offset:offset + limit])
            return SearchResponse(
                hits=sliced, total_matches=len(all_hits), offset=offset,
                has_more=(offset + len(sliced) < len(all_hits)),
                excluded_unavailable_count=excluded_unavail,
            )
        citations = self.bench.search(matter, query, limit=min(limit + offset, 20), document_ids=document_ids)
        result: list[SearchHit] = []
        for wc in citations:
            self.citations_by_key[(wc.document_id, wc.location)] = wc
            result.append(SearchHit(wc.document_id, wc.source_name, wc.location, wc.excerpt, 1.0,
                                    wc.source_version_id, wc.excerpt_digest, wc.evidence_kind))
        sliced = tuple(result[offset:offset + limit])
        return SearchResponse(hits=sliced, total_matches=len(result), offset=offset, has_more=(offset + len(sliced) < len(result)))

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
                self.citations_by_key[(wc.document_id, wc.location)] = wc
                pages.append(SourcePage(page_key, wc.location, unit.text, doc.version_id, unit.excerpt_digest))
            return SourceReadResult(doc.document_id, doc.display_name, total_pages, tuple(pages))

    def _notebook_section_rows(
        self, matter_id: str, *, limit: int, document_ids: frozenset[str] | None,
        item_types: tuple[str, ...] | None = None, include_dated: bool = False,
    ) -> list[dict[str, object]]:
        if self.allowed_notebook_items is not None:
            items = []
            for n in self.allowed_notebook_items:
                if item_types:
                    matches = (n.item_type in item_types) or (include_dated and bool((n.date_label or "").strip()))
                    if not matches:
                        continue
                elif include_dated and not (n.date_label or "").strip():
                    continue
                items.append(n)
            items = items[:limit]
        else:
            items = list(self.bench.workspace.all_notebook_items(
                matter_id, self.actor_id, include_dismissed=False, limit=limit,
                item_types=item_types, include_dated=include_dated,
            ))
        rows: list[dict[str, object]] = []
        for n in items:
            ref_ids = sorted({r.document_id for r in getattr(n, "citations", ()) if getattr(r, "document_id", "")})
            if document_ids is not None and (not ref_ids or not set(ref_ids).issubset(document_ids)):
                continue
            row: dict[str, object] = {
                "item_id": n.item_id, "type": n.item_type, "title": n.title,
                "body": n.body[:600], "status": n.status, "date_label": n.date_label,
            }
            if ref_ids:
                row["source_document_ids"] = ref_ids
            rows.append(row)
        return rows

    def get_case_knowledge(
        self, matter_id: str, *, section: str = "all", limit: int = 50,
        document_ids: frozenset[str] | None = None,
    ) -> Mapping[str, object]:
        matter = self.bench._matter_by_id(matter_id)
        output: dict[str, object] = {}
        if section in {"all", "notes"}:
            output["notes"] = self._notebook_section_rows(matter_id, limit=limit, document_ids=document_ids)
        if section in {"all", "case_theory"}:
            output["case_theory"] = self._notebook_section_rows(
                matter_id, limit=limit, document_ids=document_ids, item_types=("fact", "issue"),
            )
        if section in {"all", "timeline"}:
            output["timeline"] = self._notebook_section_rows(
                matter_id, limit=limit, document_ids=document_ids, item_types=("event", "date"), include_dated=True,
            )
        if section in {"all", "entities", "assertions"}:
            if document_ids is not None:
                if section in {"all", "entities"}:
                    output["entities"] = []
                if section in {"all", "assertions"}:
                    output["assertions"] = []
                return output
            try:
                page = MatterKnowledgeService(self.bench.assertion_service(matter)).page(matter_id, self.actor_id)
            except Exception:
                page = {"entities": {"items": []}, "assertions": {"items": []}}
            if section in {"all", "entities"}:
                output["entities"] = [
                    {"name": e.get("display_name") or e.get("canonical_name"), "type": e.get("entity_type"), "status": e.get("status")}
                    for e in page["entities"]["items"][:limit]
                ]
            if section in {"all", "assertions"}:
                output["assertions"] = [
                    {
                        "title": a.get("title"),
                        "summary": a.get("statement") or a.get("summary"),
                        "date": a.get("raw_date") or a.get("date_label"),
                        "status": a.get("status"),
                    }
                    for a in page["assertions"]["items"][:limit]
                ]
        return output


def _result_to_answer(bench, result: AssistantInvestigationResult, backend: WorkbenchToolBackend, source_fingerprint: str) -> AnswerResult:
    from .case_assistant_proposals import build_verified_proposals

    claims_payload: list[dict[str, object]] = []
    used_citations: list[object] = []
    seen_tokens: set[str] = set()

    def collect_cites(refs) -> list[dict[str, object]]:
        rows: list[dict[str, object]] = []
        for ref in refs:
            wc = backend.citations_by_key.get((ref.document_id, ref.location))
            if wc is None:
                continue
            rows.append(bench._workflow_citation_payload(wc))
            if wc.support_token not in seen_tokens:
                seen_tokens.add(wc.support_token)
                used_citations.append(wc)
        return rows

    for finding in result.findings:
        claims_payload.append({"text": finding.text, "citations": collect_cites(finding.citations),
                               "calculation_ids": list(finding.calculation_ids)})
    proposals_payload = []
    for prop in build_verified_proposals(result):
        prop_cites = collect_cites(prop.citations)
        if prop_cites:
            proposals_payload.append({
                "proposal_id": prop.proposal_id, "category": prop.category, "label": prop.label,
                "title": prop.title, "detail": prop.detail, "date_label": prop.date_label,
                "calculation_ids": list(prop.calculation_ids), "disclosure_note": prop.disclosure_note,
                "citations": prop_cites,
            })
    trace_rows = [{"call_id": rec.call_id, "tool_name": rec.tool_name, "status": rec.status,
                   "elapsed_ms": rec.elapsed_ms, "cited_count": len(rec.cited_refs), "error": rec.error}
                  for rec in result.trace]
    receipt_rows = [asdict(receipt) for receipt in result.receipts.values()]
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
            "proposals": proposals_payload,
            "trace": trace_rows, "turns_used": result.turns_used, "omitted_findings": result.omitted_findings,
        },
    }
    text_parts = [f.text for f in result.findings] if supported else [_ABSTENTION_INTRO]
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
    fingerprint = retrieval_boundary.get("source_fingerprint") or bench.workspace.source_availability_fingerprint(
        matter.matter_id, retrieval_boundary.get("source_set_id") or None,
    )
    return _result_to_answer(bench, result, backend, fingerprint)


def install_assistant_proposal_routes(app, *, bench, authorized_matter, require_csrf, auth_context) -> None:
    """Register the reviewer-gated endpoint that saves one cited proposal to matter memory."""
    from .case_assistant_proposals import CATEGORY_STORAGE

    @app.post(
        "/matters/{slug}/conversations/{conversation_id}/messages/{message_id}/assistant-proposals/{proposal_id}",
        dependencies=[Depends(require_csrf)],
    )
    def save_assistant_proposal(request: Request, slug: str, conversation_id: str, message_id: str, proposal_id: str):
        matter = authorized_matter(request, slug)
        actor = auth_context(request).principal_id
        messages = bench.workspace.messages(matter.matter_id, conversation_id)
        message = next((m for m in messages if m.message_id == message_id and m.role == "assistant"), None)
        if message is None:
            raise HTTPException(404, "Saved assistant answer not found.")
        proposals = (message.payload.get("case_assistant") or {}).get("proposals") or ()
        proposal = next((p for p in proposals if isinstance(p, Mapping) and p.get("proposal_id") == proposal_id), None)
        if proposal is None:
            raise HTTPException(404, "Assistant proposal not found.")
        category = str(proposal.get("category", ""))
        if category not in CATEGORY_STORAGE:
            raise HTTPException(400, "Unsupported proposal category.")
        citations = proposal.get("citations") or ()
        if not citations:
            raise HTTPException(409, "Proposal lacks original source support.")
        store = bench.source_store(matter)
        with store.mutation_guard():
            try:
                references = bench._saved_answer_references(matter, citations, notebook_preview=True)
            except (KeyError, TypeError, ValueError, WorkspaceProblem) as exc:
                raise HTTPException(409, "A cited original changed or was removed; rerun the assistant before saving.") from exc
            for doc_id in {str(ref.get("document_id", "")).strip() for ref in references if ref.get("document_id")}:
                try:
                    store.source_path(doc_id, verify_digest=True)
                except Exception as exc:
                    raise HTTPException(409, "A cited original changed on disk or is unavailable; rerun the assistant before saving.") from exc
            item_type, status = CATEGORY_STORAGE[category]
            detail = str(proposal.get("detail", ""))
            if proposal.get("disclosure_note"):
                detail = f"{proposal['disclosure_note']}\n\n{detail}".strip()
            try:
                item, created = bench.workspace.create_notebook_item(
                    matter.matter_id, actor, item_type=item_type, status=status,
                    title=str(proposal.get("title", "Assistant proposal")), body=detail,
                    date_label=str(proposal.get("date_label", "")), origin="answer",
                    source_conversation_id=conversation_id, source_message_id=message_id,
                    dedupe_key=f"assistant-proposal:{message_id}:{proposal_id}",
                    references=references,
                )
            except WorkspaceProblem as exc:
                raise HTTPException(409, str(exc)) from exc
        if "application/json" in request.headers.get("accept", ""):
            return JSONResponse({"item_id": item.item_id, "created": created, "status": item.status, "item_type": item.item_type})
        return RedirectResponse(f"/matters/{slug}?conversation={conversation_id}#latest", status_code=303)
