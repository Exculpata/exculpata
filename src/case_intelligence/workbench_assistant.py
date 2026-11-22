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
        if section in {"all", "notes", "case_theory", "timeline"}:
            notes = self.bench.workspace.all_notebook_items(matter_id, self.actor_id, include_dismissed=False, limit=limit)
            rows = [{"item_id": n.item_id, "type": n.item_type, "title": n.title,
                     "body": n.body[:600], "status": n.status, "date_label": n.date_label} for n in notes]
            if section in {"all", "notes"}:
                output["notes"] = rows
            if section in {"all", "case_theory"}:
                output["case_theory"] = [r for r in rows if r["type"] in {"fact", "issue"}]
            if section in {"all", "timeline"}:
                output["timeline"] = [r for r in rows if r["type"] in {"event", "date"} or r["date_label"]]
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

    def read_email_thread(
        self, matter_id: str, document_id: str, *, limit: int = 25,
        allowed_document_ids: frozenset[str] | None = None,
    ) -> Mapping[str, object] | None:
        from .source_email_threads import group_threads, read_eml
        matter = self.bench._matter_by_id(matter_id)
        store = self.bench.source_store(matter)
        with store._lock:
            try:
                target = store.get(document_id)
            except KeyError:
                return None
            records, by_source = [], {}
            for doc in store.documents.values():
                if allowed_document_ids is not None and doc.document_id not in allowed_document_ids:
                    continue
                if doc.stored_name and (doc.display_name.casefold().endswith(".eml") or "rfc822" in doc.media_type.casefold()):
                    path = store.files / doc.stored_name
                    if path.is_file():
                        rec = read_eml(path)
                        records.append(rec)
                        by_source[rec.source] = doc
            target_path = str(store.files / target.stored_name) if target.stored_name else ""
            matched_nodes = ()
            for thread in group_threads(records):
                if any(node.message.source == target_path for node in thread.messages):
                    matched_nodes = thread.messages[:limit]
                    break
            messages = []
            for node in matched_nodes:
                doc = by_source.get(node.message.source)
                if doc is None:
                    continue
                units = list(enumerate(doc.iter_parsed_units(), 1))
                loc = "Section 1"
                if units:
                    wc = self._unit_citation(matter, store, doc, units[0][0], units[0][1])
                    self.citations_by_key[(wc.document_id, wc.location)] = wc
                    loc = wc.location
                messages.append({
                    "document_id": doc.document_id, "source_name": doc.display_name, "location": loc,
                    "source_version_id": doc.version_id, "subject": node.message.subject,
                    "body": node.message.body[:4_000], "weak_link": node.weak_link,
                })
            return {"document_id": target.document_id, "count": len(messages), "messages": messages}

    def read_transcript_window(
        self, matter_id: str, document_id: str, *, start_seconds: float = 0.0,
        end_seconds: float | None = None, limit: int = 50,
    ) -> Mapping[str, object] | None:
        import re
        ts_re = re.compile(r"\[(\d{1,2}):(\d{2})(?::(\d{2}))?\]\s*(?:([^:\n]{1,40}):\s*)?(.*)")
        matter = self.bench._matter_by_id(matter_id)
        store = self.bench.source_store(matter)
        with store._lock:
            try:
                doc = store.get(document_id)
            except KeyError:
                return None
            cues: list[dict[str, object]] = []
            for ordinal, unit in enumerate(doc.iter_parsed_units(), 1):
                wc = self._unit_citation(matter, store, doc, ordinal, unit)
                if unit.start_ms is not None:
                    s_sec = unit.start_ms / 1000.0
                    e_sec = (unit.end_ms / 1000.0) if unit.end_ms is not None else s_sec
                    if e_sec >= start_seconds and (end_seconds is None or s_sec <= end_seconds):
                        self.citations_by_key[(wc.document_id, wc.location)] = wc
                        cues.append({"start_seconds": s_sec, "end_seconds": e_sec, "speaker": unit.speaker_cluster,
                                     "text": unit.text, "location": wc.location, "source_version_id": doc.version_id})
                else:
                    for line in unit.text.splitlines():
                        m = ts_re.match(line.strip())
                        if not m:
                            continue
                        a, b, c = int(m.group(1)), int(m.group(2)), int(m.group(3)) if m.group(3) else None
                        sec = float(a * 3600 + b * 60 + c) if c is not None else float(a * 60 + b)
                        if sec >= start_seconds and (end_seconds is None or sec <= end_seconds):
                            self.citations_by_key[(wc.document_id, wc.location)] = wc
                            cues.append({"start_seconds": sec, "end_seconds": sec, "speaker": (m.group(4) or "").strip(),
                                         "text": m.group(5).strip(), "location": wc.location, "source_version_id": doc.version_id})
                if len(cues) >= limit:
                    break
            return {"document_id": doc.document_id, "source_name": doc.display_name,
                    "start_seconds": start_seconds, "end_seconds": end_seconds, "count": len(cues), "cues": cues[:limit]}


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
            "proposals": proposals_payload,
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
        with bench.source_store(matter).mutation_guard():
            for raw in citations:
                try:
                    wc = bench._workflow_citation(raw)
                except (KeyError, TypeError, ValueError) as exc:
                    raise HTTPException(409, "A cited original changed or is unavailable.") from exc
                if bench._current_workflow_citation(matter, wc) is None:
                    raise HTTPException(409, "A cited original changed or was removed; rerun the assistant before saving.")
            item_type, status = CATEGORY_STORAGE[category]
            detail = str(proposal.get("detail", ""))
            if proposal.get("disclosure_note"):
                detail = f"{proposal['disclosure_note']}\n\n{detail}".strip()
            item, created = bench.workspace.create_notebook_item(
                matter.matter_id, actor, item_type=item_type, status=status,
                title=str(proposal.get("title", "Assistant proposal")), body=detail,
                date_label=str(proposal.get("date_label", "")), origin="answer",
                source_conversation_id=conversation_id, source_message_id=message_id,
                dedupe_key=f"assistant-proposal:{message_id}:{proposal_id}",
                references=citations,
            )
        if "application/json" in request.headers.get("accept", ""):
            return JSONResponse({"item_id": item.item_id, "created": created, "status": item.status, "item_type": item.item_type})
        return RedirectResponse(f"/matters/{slug}?conversation={conversation_id}#latest", status_code=303)
