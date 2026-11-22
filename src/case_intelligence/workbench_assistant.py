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
    AssistantAccessError, MatterToolRuntime, SearchHit, SourcePage, SourceReadResult, SourceSummary,
)
from .exact_search_results import ExactSearchUnavailable
from .generation import OllamaGenerator, OpenAICompatibleGenerator
from .generation_errors import GenerationUnavailable
from .generation_matter import generation_matter_scope
from .matter_knowledge import MatterKnowledgeService
from .review_bench_v2 import Candidate

_TRUTHY = frozenset({"1", "true", "yes", "on"})


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

    def __init__(self, bench, actor_id: str) -> None:
        self.bench = bench
        self.actor_id = actor_id
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
        offset: int = 0, limit: int = 50,
    ) -> tuple[Sequence[SourceSummary], int]:
        matter = self.bench._matter_by_id(matter_id)
        store = self.bench.source_store(matter)
        with store._lock:
            docs = list(store.documents.values())
        rows: list[SourceSummary] = []
        needle = query.strip().casefold()
        for doc in docs:
            norm_status = "ready" if doc.state == "Searchable" else "failed"
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
        limit: int = 20, document_ids: frozenset[str] | None = None,
    ) -> Sequence[SearchHit]:
        matter = self.bench._matter_by_id(matter_id)
        if mode == "exact":
            try:
                page = self.bench.exact_search(matter, query, page_size=25 if limit <= 25 else 50)
            except (ValueError, ExactSearchUnavailable):
                return ()
            store = self.bench.source_store(matter)
            hits: list[SearchHit] = []
            with store._lock:
                for item in page.items:
                    if document_ids is not None and item.document_id not in document_ids:
                        continue
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
                        hits.append(SearchHit(wc.document_id, wc.source_name, wc.location, wc.excerpt, 1.0,
                                              wc.source_version_id, wc.excerpt_digest, wc.evidence_kind))
                        if len(hits) >= limit:
                            return hits
            return hits
        citations = self.bench.search(matter, query, limit=min(limit, 20), document_ids=document_ids)
        result: list[SearchHit] = []
        for wc in citations:
            self.citations_by_key[(wc.document_id, wc.location)] = wc
            result.append(SearchHit(wc.document_id, wc.source_name, wc.location, wc.excerpt, 1.0,
                                    wc.source_version_id, wc.excerpt_digest, wc.evidence_kind))
        return result

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
            total = len(all_units)
            stop = total if end_page is None else min(end_page, total)
            pages: list[SourcePage] = []
            for ordinal, unit in all_units[max(start_page - 1, 0):stop]:
                wc = self._unit_citation(matter, store, doc, ordinal, unit)
                self.citations_by_key[(wc.document_id, wc.location)] = wc
                pages.append(SourcePage(ordinal, wc.location, unit.text, doc.version_id, unit.excerpt_digest))
            return SourceReadResult(doc.document_id, doc.display_name, total, tuple(pages))

    def get_case_knowledge(self, matter_id: str, *, section: str = "all", limit: int = 50) -> Mapping[str, object]:
        matter = self.bench._matter_by_id(matter_id)
        output: dict[str, object] = {}
        if section in {"all", "notes"}:
            notes = self.bench.workspace.all_notebook_items(matter_id, self.actor_id, include_dismissed=False, limit=limit)
            output["notes"] = [{"title": n.title, "body": n.body[:600], "status": n.status, "date_label": n.date_label} for n in notes]
        if section in {"all", "entities", "assertions"}:
            try:
                page = MatterKnowledgeService(self.bench.assertion_service(matter)).page(matter_id, self.actor_id)
            except Exception:
                page = {"entities": {"items": []}, "assertions": {"items": []}}
            if section in {"all", "entities"}:
                output["entities"] = [{"name": e.get("canonical_name"), "type": e.get("entity_type"), "status": e.get("status")}
                                      for e in page["entities"]["items"][:limit]]
            if section in {"all", "assertions"}:
                output["assertions"] = [{"summary": a.get("summary"), "date": a.get("date_label"), "status": a.get("status")}
                                        for a in page["assertions"]["items"][:limit]]
        return output


def _result_to_answer(bench, result: AssistantInvestigationResult, backend: WorkbenchToolBackend, source_fingerprint: str) -> AnswerResult:
    claims_payload: list[dict[str, object]] = []
    used_citations: list[object] = []
    seen_tokens: set[str] = set()
    for finding in result.findings:
        cite_rows: list[dict[str, object]] = []
        for ref in finding.citations:
            wc = backend.citations_by_key.get((ref.document_id, ref.location))
            if wc is None:
                continue
            cite_rows.append(bench._workflow_citation_payload(wc))
            if wc.support_token not in seen_tokens:
                seen_tokens.add(wc.support_token)
                used_citations.append(wc)
        claims_payload.append({"text": finding.text, "citations": cite_rows, "calculation_ids": list(finding.calculation_ids)})
    trace_rows = [{"call_id": rec.call_id, "tool_name": rec.tool_name, "status": rec.status,
                   "elapsed_ms": rec.elapsed_ms, "cited_count": len(rec.cited_refs), "error": rec.error}
                  for rec in result.trace]
    receipt_rows = [asdict(receipt) for receipt in result.receipts.values()]
    payload: dict[str, object] = {
        "kind": "generated" if result.answerable and claims_payload else "not-supported",
        "answerable": bool(result.answerable and claims_payload),
        "model_called": True,
        "introduction": result.summary,
        "claims": claims_payload,
        "limitation": None,
        "missing_information": "" if result.answerable and claims_payload else result.summary,
        "case_assistant": {
            "receipts": receipt_rows, "gaps": list(result.gaps),
            "competing_accounts": list(result.competing_accounts),
            "trace": trace_rows, "turns_used": result.turns_used, "omitted_findings": result.omitted_findings,
        },
    }
    text_parts = [result.summary, *(f.text for f in result.findings)]
    return AnswerResult("\n".join(p for p in text_parts if p), payload, tuple(used_citations),
                        retrieval_source_fingerprint=source_fingerprint)


def run_workbench_assistant_job(
    bench, job, matter, *, prior: Sequence[object], scoped_document_ids: frozenset[str] | None,
    retrieval_boundary: Mapping[str, str], report_stage: Callable[[str, str], None],
    cancelled: Callable[[], bool],
) -> AnswerResult:
    """Execute one AnswerJobRecord through the tool-using CaseAssistant."""
    client, tool_style = assistant_client_from_generator(getattr(bench.generator, "client", None))
    backend = WorkbenchToolBackend(bench, job.actor_id)
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
