"""App-shell adapter wiring CaseAssistant and MatterToolRuntime into WorkbenchService."""
from __future__ import annotations

from dataclasses import asdict, replace
import os
from typing import Callable, Mapping, Sequence

from fastapi import Depends, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse

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

    def _document_bates_map(self, matter_id: str, doc_ids: Sequence[str]) -> dict[str, str]:
        connection = getattr(self.bench.workspace, "connection", None)
        lock = getattr(self.bench.workspace, "_lock", None)
        if connection is None or lock is None or not doc_ids:
            return {}
        from .modules import ModuleDatabase
        from .modules.discovery_review.identifiers import IdentifierRepository
        from .modules.discovery_review.production import ProductionRepository

        db = ModuleDatabase(connection, lock)
        repo = ProductionRepository(db)
        labels = repo.document_bates_ranges(matter_id, doc_ids)
        assigned = IdentifierRepository(db, self.bench.workspace.membership).read(matter_id)
        for doc_id in doc_ids:
            if doc_id not in labels and doc_id in assigned and repo.document_status(matter_id, doc_id) == "complete":
                labels[doc_id] = f"Team ID: {assigned[doc_id]}"
        return labels

    def list_sources(
        self, matter_id: str, *, query: str = "", kind: str = "", status: str = "",
        offset: int = 0, limit: int = 50, document_ids: frozenset[str] | None = None,
    ) -> tuple[Sequence[SourceSummary], int]:
        matter = self.bench._matter_by_id(matter_id)
        store = self.bench.source_store(matter)
        with store._lock:
            docs = list(store.documents.values())
        filtered = []
        for doc in docs:
            if document_ids is not None and doc.document_id not in document_ids:
                continue
            norm_status = "ready" if doc.state == "ready" else "failed"
            doc_kind = doc.media_type.split("/")[-1] if "/" in doc.media_type else (doc.media_type or "document")
            if status and norm_status != status:
                continue
            if kind and kind.casefold() not in (doc_kind.casefold(), doc.media_type.casefold()):
                continue
            filtered.append((doc, norm_status, doc_kind))
        filtered.sort(key=lambda item: (item[0].display_name.casefold(), item[0].document_id))
        needle = query.strip().casefold()
        if not needle:
            window = filtered[offset:offset + limit]
            bates_map = self._document_bates_map(matter_id, [doc.document_id for doc, _, _ in window])
            rows = [
                SourceSummary(
                    document_id=doc.document_id, source_name=doc.display_name, kind=doc_kind,
                    status=norm_status, page_count=doc.page_count or doc.total_units,
                    path=doc.relative_path, bates=bates_map.get(doc.document_id, ""),
                    status_detail="" if norm_status == "ready" else (doc.message or doc.state),
                )
                for doc, norm_status, doc_kind in window
            ]
            return rows, len(filtered)
        bates_map = self._document_bates_map(matter_id, [doc.document_id for doc, _, _ in filtered])
        matched: list[SourceSummary] = []
        for doc, norm_status, doc_kind in filtered:
            bates = bates_map.get(doc.document_id, "")
            if needle not in (doc.display_name + " " + doc.relative_path + " " + bates).casefold():
                continue
            matched.append(SourceSummary(
                document_id=doc.document_id, source_name=doc.display_name, kind=doc_kind,
                status=norm_status, page_count=doc.page_count or doc.total_units,
                path=doc.relative_path, bates=bates,
                status_detail="" if norm_status == "ready" else (doc.message or doc.state),
            ))
        return matched[offset:offset + limit], len(matched)

    def inspect_production(
        self, matter_id: str, *, load_file_document_id: str = "", include_gaps: bool = True,
        document_ids: frozenset[str] | None = None,
    ) -> Mapping[str, object]:
        from collections import Counter
        import csv
        from pathlib import PurePosixPath
        from . import production_summary as summary
        from .modules import ModuleDatabase
        from .modules.discovery_review.production import ProductionRepository
        from .pilot_uploads import UploadProblem

        matter = self.bench._matter_by_id(matter_id)
        store = self.bench.source_store(matter)
        with store._lock:
            docs = [d for d in store.documents.values() if document_ids is None or d.document_id in document_ids]
        by_doc_id = {d.document_id: d for d in docs}
        media_counts = dict(sorted(Counter(d.media_type for d in docs).items()))
        pdf_pages = sum(int(d.page_count or 0) for d in docs if d.media_type == "application/pdf")
        unavail = [
            {"document_id": d.document_id, "source_name": d.display_name, "state": d.state,
             "status_detail": d.message or d.state}
            for d in docs if d.state != "ready"
        ]
        load_suffixes = frozenset({".csv", ".dat", ".opt", ".lfp", ".dii"})
        load_files = [
            {"document_id": d.document_id, "source_name": d.display_name,
             "relative_path": d.relative_path, "state": d.state}
            for d in docs if PurePosixPath(d.relative_path.replace("\\", "/")).suffix.casefold() in load_suffixes
        ]
        attempts: list[dict[str, object]] = []
        attempts_truncated = False
        connection = getattr(self.bench.workspace, "connection", None)
        lock = getattr(self.bench.workspace, "_lock", None)
        if document_ids is None and connection is not None and lock is not None:
            repo = ProductionRepository(ModuleDatabase(connection, lock))
            raw_attempts = list(repo.attempts(matter_id, limit=51))
            attempts_truncated = len(raw_attempts) > 50
            raw_attempts = raw_attempts[:50]
            if load_file_document_id and not any(a.get("document_id") == load_file_document_id for a in raw_attempts):
                targeted = repo.current_attempt(matter_id, load_file_document_id)
                if targeted is not None:
                    raw_attempts.append(targeted)
            for att in raw_attempts:
                attempts.append({
                    "attempt_id": att.get("attempt_id", ""),
                    "document_id": att.get("document_id", ""),
                    "source_name": att.get("name", ""),
                    "source_format": att.get("source_format", ""),
                    "status": att.get("status", ""),
                    "counts": dict(att.get("counts") or {}),
                    "failure": att.get("file_failure") or "",
                })
        registered_refs: dict[str, dict[str, str]] = {}

        def _register_doc_ref(doc) -> dict[str, str] | None:
            if doc is None or doc.state != "ready":
                return None
            if doc.document_id in registered_refs:
                return registered_refs[doc.document_id]
            first_unit = next(iter(doc.iter_parsed_units()), None)
            if first_unit is None:
                return None
            wc = self._unit_citation(matter, store, doc, 1, first_unit)
            self._remember_citation(wc)
            ref = {"document_id": wc.document_id, "source_name": wc.source_name, "location": wc.location,
                   "source_version_id": wc.source_version_id, "excerpt_digest": wc.excerpt_digest}
            registered_refs[doc.document_id] = ref
            return ref

        bates_gaps: dict[str, object] = {"computed": False, "notice": summary.NOTICE, "counts": {}, "facts": []}
        target_doc = None
        if include_gaps:
            if document_ids is not None:
                bates_gaps["reason"] = "Source-set filter active; load-file attempt coverage and Bates gap computation require full-matter scope."
            else:
                if load_file_document_id:
                    target_doc = next((d for d in docs if d.document_id == load_file_document_id), None)
                    if target_doc is None:
                        raise AssistantToolError("Requested load file was not found in this matter.")
                else:
                    csv_docs = [d for d in docs if d.relative_path.lower().endswith(".csv")]
                    if len(csv_docs) == 1:
                        target_doc = csv_docs[0]
                if target_doc is not None:
                    try:
                        built = summary.build_summary(matter.display_name, matter.slug, store, store.action_token(target_doc))
                        load_ref = _register_doc_ref(target_doc)
                        fact_rows = []
                        for fact in built["facts"][:100]:
                            f_refs = [ref for cid, _ in (fact.get("cited_ids") or ()) if (ref := _register_doc_ref(by_doc_id.get(cid)))]
                            if load_ref is not None and not any(r["document_id"] == load_ref["document_id"] for r in f_refs):
                                f_refs.insert(0, load_ref)
                            fact_rows.append({"kind": fact["kind"], "text": fact["text"], "source_refs": f_refs})
                        bates_gaps = {
                            "computed": True, "load_file_document_id": target_doc.document_id,
                            "load_file_name": target_doc.display_name, "load_file_ref": load_ref,
                            "notice": built["notice"], "counts": dict(built["counts"]),
                            "facts": fact_rows, "truncated": len(built["facts"]) > 100,
                        }
                    except (ValueError, csv.Error, UnicodeDecodeError, WorkspaceProblem, UploadProblem) as exc:
                        bates_gaps["reason"] = f"Bates gaps could not be computed for {target_doc.display_name}: {exc}"
        source_refs = [ref for doc in sorted(docs, key=lambda item: (item.display_name.casefold(), item.document_id))[:50] if (ref := _register_doc_ref(doc))]
        if target_doc is not None and not any(r["document_id"] == target_doc.document_id for r in source_refs) and (ref := _register_doc_ref(target_doc)):
            source_refs.insert(0, ref)
        return {
            "total_sources": len(docs), "counts_by_media_type": media_counts, "recorded_pdf_pages": pdf_pages,
            "unavailable_count": len(unavail), "unavailable_sources": unavail[:50], "load_files": load_files[:50],
            "load_file_attempts": attempts, "load_file_attempts_truncated": attempts_truncated,
            "bates_gaps": bates_gaps, "source_refs": source_refs,
        }

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

    def _notebook_section_rows(
        self, matter_id: str, *, limit: int, offset: int = 0, document_ids: frozenset[str] | None,
        item_types: tuple[str, ...] | None = None, include_dated: bool = False,
    ) -> tuple[list[dict[str, object]], int, int, bool]:
        if self.allowed_notebook_items is not None:
            raw_items = []
            for n in self.allowed_notebook_items:
                if item_types:
                    matches = (n.item_type in item_types) or (include_dated and bool((n.date_label or "").strip()))
                    if not matches:
                        continue
                elif include_dated and not (n.date_label or "").strip():
                    continue
                raw_items.append(n)
            filtered = []
            for n in raw_items:
                ref_ids = sorted({r.document_id for r in getattr(n, "citations", ()) if getattr(r, "document_id", "")})
                if document_ids is not None and (not ref_ids or not set(ref_ids).issubset(document_ids)):
                    continue
                filtered.append((n, ref_ids))
            total = len(filtered)
            page_items = filtered[offset:offset + limit]
            omitted = max(0, total - (offset + len(page_items)))
            has_more = omitted > 0
        else:
            fetch_cap = offset + limit + 1
            raw_items = list(self.bench.workspace.all_notebook_items(
                matter_id, self.actor_id, include_dismissed=False, limit=fetch_cap,
                item_types=item_types, include_dated=include_dated,
            ))
            filtered = []
            for n in raw_items:
                ref_ids = sorted({r.document_id for r in getattr(n, "citations", ()) if getattr(r, "document_id", "")})
                if document_ids is not None and (not ref_ids or not set(ref_ids).issubset(document_ids)):
                    continue
                filtered.append((n, ref_ids))
            has_more = len(filtered) > offset + limit
            page_items = filtered[offset:offset + limit]
            total = len(filtered) if not has_more else offset + len(page_items) + 1
            omitted = max(0, total - (offset + len(page_items)))
        rows: list[dict[str, object]] = []
        for n, ref_ids in page_items:
            item_id = getattr(n, "item_id", None) or getattr(n, "notebook_item_id", "")
            row: dict[str, object] = {
                "item_id": item_id,
                "type": getattr(n, "item_type", "note"),
                "title": getattr(n, "title", ""),
                "body": str(getattr(n, "body", ""))[:600],
                "status": getattr(n, "status", "confirmed"),
                "date_label": getattr(n, "date_label", ""),
            }
            if ref_ids:
                row["source_document_ids"] = ref_ids
            rows.append(row)
        return rows, total, omitted, has_more

    def get_case_knowledge(
        self, matter_id: str, *, section: str = "all", limit: int = 50, offset: int = 0,
        document_ids: frozenset[str] | None = None,
    ) -> Mapping[str, object]:
        matter = self.bench._matter_by_id(matter_id)
        output: dict[str, object] = {"offset": offset, "limit": limit}
        any_has_more = False
        if section in {"all", "notes"}:
            note_rows, notes_total, notes_omitted, notes_has_more = self._notebook_section_rows(
                matter_id, limit=limit, offset=offset, document_ids=document_ids,
            )
            output["notes"] = note_rows
            output["notes_total"] = notes_total
            output["notes_omitted"] = notes_omitted
            output["notes_has_more"] = notes_has_more
            any_has_more = any_has_more or notes_has_more
        if section in {"all", "case_theory"}:
            theory_rows, theory_total, theory_omitted, theory_has_more = self._notebook_section_rows(
                matter_id, limit=limit, offset=offset, document_ids=document_ids, item_types=("fact", "issue"),
            )
            output["case_theory"] = theory_rows
            output["case_theory_total"] = theory_total
            output["case_theory_omitted"] = theory_omitted
            output["case_theory_has_more"] = theory_has_more
            any_has_more = any_has_more or theory_has_more
        if section in {"all", "timeline"}:
            tl_rows, tl_total, tl_omitted, tl_has_more = self._notebook_section_rows(
                matter_id, limit=limit, offset=offset, document_ids=document_ids,
                item_types=("event", "date"), include_dated=True,
            )
            output["timeline"] = tl_rows
            output["timeline_total"] = tl_total
            output["timeline_omitted"] = tl_omitted
            output["timeline_has_more"] = tl_has_more
            any_has_more = any_has_more or tl_has_more
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


    def read_structured_table(
        self, matter_id: str, document_id: str, *, sheet: str = "",
    ):
        """Parse CSV/TSV/XLSX directly from the digest-verified original file with exact per-section citations."""
        import csv
        import io
        from pathlib import Path
        import re
        from .case_assistant_tools import AssistantToolError, SourceRef
        from .extended_extract import (
            MAX_COLUMNS, MAX_ROWS, MAX_TEXT_CONTAINER_BYTES, _cell_text, extract_xlsx_structured,
        )

        matter = self.bench._matter_by_id(matter_id)
        store = self.bench.source_store(matter)
        with store._lock:
            try:
                doc = store.get(document_id)
            except KeyError:
                return None
            suffix = Path(doc.display_name).suffix.casefold()
            media = (doc.media_type or "").casefold()
            is_csv = suffix == ".csv" or media == "text/csv"
            is_tsv = suffix == ".tsv" or media == "text/tab-separated-values"
            is_xlsx = suffix == ".xlsx" or "spreadsheetml" in media
            if not (is_csv or is_tsv or is_xlsx):
                return None
            if doc.origin != "upload":
                return None
            try:
                verified_path = store.source_path(doc.document_id, verify_digest=True)
            except Exception as exc:
                raise AssistantToolError(f"Original table integrity check failed for {doc.display_name}.") from exc
            all_units = list(enumerate(doc.iter_parsed_units(), 1))
            if not all_units or len(all_units) > 500:
                raise AssistantToolError("Table source exceeds complete-aggregation coverage bounds.")
            unit_meta: list[tuple[str | None, int, int, object]] = []
            refs: list[SourceRef] = []
            row_range_re = re.compile(r"^(?:(.+?),\s*)?rows\s+(\d+)[–-](\d+)", re.IGNORECASE)
            for ordinal, unit in all_units:
                wc = self._unit_citation(matter, store, doc, ordinal, unit)
                self._remember_citation(wc)
                refs.append(SourceRef(wc.document_id, wc.source_name, wc.location, wc.source_version_id, wc.excerpt_digest))
                m = row_range_re.match((unit.location_label or wc.location).strip())
                if m:
                    unit_meta.append(((m.group(1) or "").strip() or None, int(m.group(2)), int(m.group(3)), wc))
                else:
                    unit_meta.append((None, (ordinal - 1) * 100 + 1, ordinal * 100, wc))

            def loc_for(row_num: int, sheet_name: str | None = None) -> str:
                for u_sheet, start_r, end_r, wc in unit_meta:
                    if sheet_name is not None and (u_sheet or "").casefold() != sheet_name.casefold():
                        continue
                    if start_r <= row_num <= end_r:
                        return wc.location
                return unit_meta[-1][3].location

            if is_csv or is_tsv:
                if verified_path.stat().st_size > MAX_TEXT_CONTAINER_BYTES:
                    raise AssistantToolError("Table source exceeds complete-aggregation coverage bounds.")
                raw = verified_path.read_bytes()
                try:
                    text = raw.decode("utf-8-sig", errors="strict")
                    rows_iter = csv.reader(io.StringIO(text), delimiter="\t" if is_tsv else ",", strict=True)
                    header_vals: list[str] | None = None
                    header_row_num = 1
                    tagged: list[tuple[dict[str, str], str, str]] = []
                    for row_num, row in enumerate(rows_iter, 1):
                        if row_num > MAX_ROWS:
                            raise AssistantToolError("Table source exceeds complete-aggregation coverage bounds.")
                        vals = [_cell_text(v) for v in row]
                        if len(vals) > MAX_COLUMNS:
                            raise AssistantToolError("Table source exceeds supported column limit.")
                        if not any(vals):
                            continue
                        if header_vals is None:
                            from .case_assistant_tools import _ensure_unique_headers
                            _ensure_unique_headers(vals)
                            header_vals = vals
                            header_row_num = row_num
                            continue
                        row_dict = {col: (vals[idx] if idx < len(vals) else "") for idx, col in enumerate(header_vals) if col}
                        tagged.append((row_dict, loc_for(row_num), doc.version_id))
                except (UnicodeDecodeError, csv.Error) as exc:
                    raise AssistantToolError(f"Malformed delimited table {doc.display_name}: {exc}") from exc
                columns = [h for h in (header_vals or []) if h]
                return doc.display_name, columns, tagged, refs, loc_for(header_row_num)

            try:
                chosen_name, columns, sheet_rows, header_row = extract_xlsx_structured(verified_path, sheet=sheet)
            except ValueError as exc:
                raise AssistantToolError(str(exc)) from exc
            tagged = [(row_dict, loc_for(sheet_row, chosen_name), doc.version_id) for row_dict, sheet_row in sheet_rows]
            return doc.display_name, columns, tagged, refs, loc_for(header_row, chosen_name)

    def read_email_thread(
        self, matter_id: str, document_id: str, *, limit: int = 25, offset: int | None = None,
        allowed_document_ids: frozenset[str] | None = None,
    ) -> Mapping[str, object] | None:
        from .case_assistant_tools import AssistantToolError
        from .source_email_threads import group_threads, read_eml
        matter = self.bench._matter_by_id(matter_id)
        store = self.bench.source_store(matter)
        with store._lock:
            try:
                target = store.get(document_id)
            except KeyError:
                return None
            try:
                target_verified = store.source_path(target.document_id, verify_digest=True)
            except Exception as exc:
                raise AssistantToolError(f"Original email integrity check failed for {target.display_name}.") from exc
            records, by_source = [], {}
            unthreaded_errors: list[dict[str, str]] = []
            for doc in store.documents.values():
                if allowed_document_ids is not None and doc.document_id not in allowed_document_ids:
                    continue
                if doc.stored_name and (doc.display_name.casefold().endswith(".eml") or "rfc822" in doc.media_type.casefold()):
                    try:
                        verified_path = store.source_path(doc.document_id, verify_digest=True)
                    except Exception as exc:
                        raise AssistantToolError(f"Original email integrity check failed for {doc.display_name}.") from exc
                    if verified_path.is_file():
                        rec = read_eml(verified_path)
                        if rec.error:
                            if doc.document_id == target.document_id:
                                raise AssistantToolError(
                                    f"Email headers/thread links could not be parsed for {doc.display_name}: {rec.error}"
                                )
                            unthreaded_errors.append({
                                "document_id": doc.document_id,
                                "source_name": doc.display_name,
                                "error": str(rec.error),
                            })
                            continue
                        records.append(rec)
                        by_source[rec.source] = doc
            target_path = str(target_verified)
            full_thread = ()
            target_index = 0
            for thread in group_threads(records):
                for idx, node in enumerate(thread.messages):
                    if node.message.source == target_path:
                        full_thread = thread.messages
                        target_index = idx
                        break
                if full_thread:
                    break
            total_messages = len(full_thread)
            if offset is None:
                effective_offset = max(0, target_index - limit + 1) if target_index >= limit else 0
            else:
                effective_offset = offset
            matched_nodes = full_thread[effective_offset:effective_offset + limit]
            messages = []
            any_body_truncated = False
            for node in matched_nodes:
                doc = by_source.get(node.message.source)
                if doc is None:
                    continue
                units = list(enumerate(doc.iter_parsed_units(), 1))
                body_units = [(ord_idx, u) for ord_idx, u in units if (u.location_label or "") != "Email headers"]
                chosen_units = body_units if body_units else units[:1]
                body_spans: list[dict[str, object]] = []
                msg_truncated = False
                for ord_idx, unit in chosen_units:
                    wc = self._unit_citation(matter, store, doc, ord_idx, unit)
                    self._remember_citation(wc)
                    full_len = len(unit.text)
                    snippet = unit.text[:4_000]
                    span_truncated = full_len > len(snippet)
                    msg_truncated = msg_truncated or span_truncated
                    body_spans.append({
                        "location": wc.location,
                        "page_number": ord_idx,
                        "source_version_id": doc.version_id,
                        "excerpt_digest": wc.excerpt_digest,
                        "text": snippet,
                        "total_chars": full_len,
                        "omitted_chars": full_len - len(snippet),
                        "truncated": span_truncated,
                        "next_char_offset": len(snippet) if span_truncated else None,
                    })
                any_body_truncated = any_body_truncated or msg_truncated
                primary = body_spans[0] if body_spans else {"location": "Section 1", "text": ""}
                messages.append({
                    "document_id": doc.document_id, "source_name": doc.display_name,
                    "location": primary["location"],
                    "source_version_id": doc.version_id, "subject": node.message.subject,
                    "body": primary["text"], "body_spans": body_spans,
                    "truncated": msg_truncated, "weak_link": node.weak_link,
                })
            has_more = (effective_offset + len(messages)) < total_messages
            incomplete_threading = bool(unthreaded_errors)
            truncated = (len(messages) < total_messages) or any_body_truncated or incomplete_threading
            return {
                "document_id": target.document_id,
                "count": len(messages),
                "total_messages": total_messages,
                "offset": effective_offset,
                "limit": limit,
                "has_more": has_more,
                "next_offset": (effective_offset + len(messages)) if has_more else None,
                "previous_offset": max(0, effective_offset - limit) if effective_offset > 0 else None,
                "body_truncated": any_body_truncated,
                "incomplete_threading": incomplete_threading,
                "unthreaded_email_errors": unthreaded_errors,
                "truncated": truncated,
                "messages": messages,
            }

    def read_transcript_window(
        self, matter_id: str, document_id: str, *, start_seconds: float = 0.0,
        end_seconds: float | None = None, limit: int = 50, offset: int = 0,
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
            all_cues: list[tuple[dict[str, object], list[object]]] = []
            active_cue: dict[str, object] | None = None
            active_wcs: list[object] = []
            untimestamped_omitted = 0
            for ordinal, unit in enumerate(doc.iter_parsed_units(), 1):
                wc = self._unit_citation(matter, store, doc, ordinal, unit)
                if unit.start_ms is not None:
                    active_cue = None
                    active_wcs = []
                    s_sec = unit.start_ms / 1000.0
                    e_sec = (unit.end_ms / 1000.0) if unit.end_ms is not None else s_sec
                    if e_sec >= start_seconds and (end_seconds is None or s_sec <= end_seconds):
                        all_cues.append((
                            {"start_seconds": s_sec, "end_seconds": e_sec, "speaker": unit.speaker_cluster,
                             "text": unit.text, "location": wc.location, "source_version_id": doc.version_id},
                            [wc],
                        ))
                else:
                    for raw_line in unit.text.splitlines():
                        stripped = raw_line.strip()
                        if not stripped:
                            continue
                        m = ts_re.match(stripped)
                        if m:
                            a, b, c = int(m.group(1)), int(m.group(2)), int(m.group(3)) if m.group(3) else None
                            sec = float(a * 3600 + b * 60 + c) if c is not None else float(a * 60 + b)
                            cue_text = m.group(5).strip()
                            cue_dict: dict[str, object] = {
                                "start_seconds": sec,
                                "end_seconds": sec,
                                "speaker": (m.group(4) or "").strip(),
                                "text": cue_text,
                                "location": wc.location,
                                "source_version_id": doc.version_id,
                                "spans": [{"location": wc.location, "text": cue_text}],
                            }
                            active_cue = cue_dict
                            active_wcs = [wc]
                            if sec >= start_seconds and (end_seconds is None or sec <= end_seconds):
                                all_cues.append((cue_dict, active_wcs))
                        elif active_cue is not None:
                            prev = str(active_cue.get("text") or "")
                            active_cue["text"] = f"{prev}\n{stripped}".strip() if prev else stripped
                            spans = active_cue.get("spans")
                            if isinstance(spans, list):
                                if spans and spans[-1].get("location") == wc.location:
                                    s_prev = str(spans[-1].get("text") or "")
                                    spans[-1]["text"] = f"{s_prev}\n{stripped}".strip() if s_prev else stripped
                                else:
                                    spans.append({"location": wc.location, "text": stripped})
                            if wc.location != active_cue["location"]:
                                cont_locs = active_cue.setdefault("continuation_locations", [])
                                if isinstance(cont_locs, list) and wc.location not in cont_locs:
                                    cont_locs.append(wc.location)
                                if not any(existing.location == wc.location for existing in active_wcs):
                                    active_wcs.append(wc)
                        else:
                            untimestamped_omitted += 1
            total_cues = len(all_cues)
            sliced = all_cues[offset:offset + limit]
            page_cues: list[dict[str, object]] = []
            for cue, wcs in sliced:
                for wc in wcs:
                    self._remember_citation(wc)
                page_cues.append(cue)
            omitted_cues = max(0, total_cues - (offset + len(page_cues)))
            has_more = omitted_cues > 0
            return {
                "document_id": doc.document_id,
                "source_name": doc.display_name,
                "start_seconds": start_seconds,
                "end_seconds": end_seconds,
                "offset": offset,
                "limit": limit,
                "count": len(page_cues),
                "total_cues": total_cues,
                "omitted_cues": omitted_cues,
                "untimestamped_lines_omitted": untimestamped_omitted,
                "has_more": has_more,
                "next_offset": (offset + len(page_cues)) if has_more else None,
                "truncated": (len(page_cues) < total_cues) or (untimestamped_omitted > 0),
                "cues": page_cues,
            }

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
        receipt_cites: list[dict[str, object]] = []
        seen_receipt_tokens: set[str] = set()
        for wc in premise_wcs:
            assert wc is not None
            if wc.support_token not in seen_tokens:
                seen_tokens.add(wc.support_token)
                used_citations.append(wc)
            if wc.support_token not in seen_receipt_tokens:
                seen_receipt_tokens.add(wc.support_token)
                receipt_cites.append(bench._saved_answer_citation_payload(wc))
        receipt_rows.append({
            "receipt_id": receipt.receipt_id,
            "operation": receipt.operation,
            "expression": _verified_receipt_expression(receipt),
            "result": receipt.result,
            "unit": _verified_receipt_unit(receipt, premise_wcs),
            "citations": receipt_cites,
            "inputs": [
                {"value": inp.value, "source_refs": [asdict(sref) for sref in inp.source_refs]}
                for inp in receipt.inputs
            ],
        })
    import re
    from .case_assistant import _extract_nums

    allowed_nums: set[str] = set()
    for wc in used_citations:
        allowed_nums.update(_extract_nums(str(getattr(wc, "excerpt", "") or "")))
    for r in receipt_rows:
        allowed_nums.update(_extract_nums(str(r.get("result", ""))))
    verified_competing: list[str] = []
    if used_citations:
        for account in result.competing_accounts:
            text = str(account or "").strip()
            if not text:
                continue
            nums = _extract_nums(text)
            if nums and not nums.issubset(allowed_nums):
                continue
            verified_competing.append(text)
    _system_gap_re = re.compile(
        r"^(?:\d+\s+unavailable\b|Turn budget\b|Model returned\b|Investigation\b)",
        re.IGNORECASE,
    )
    verified_gaps: list[str] = []
    for gap in result.gaps:
        text = str(gap or "").strip()
        if not text:
            continue
        if _system_gap_re.match(text):
            verified_gaps.append(text)
            continue
        nums = _extract_nums(text)
        if nums and not nums.issubset(allowed_nums):
            continue
        verified_gaps.append(text)
    from .case_assistant_proposals import build_verified_proposals

    verified_receipts = {
        str(r["receipt_id"]): replace(
            result.receipts[str(r["receipt_id"])],
            expression=str(r["expression"]),
            unit=str(r["unit"]),
        )
        for r in receipt_rows
        if str(r["receipt_id"]) in result.receipts
    }
    verified_result = replace(result, findings=tuple(retained_findings), receipts=verified_receipts)
    proposals_payload: list[dict[str, object]] = []
    for prop in build_verified_proposals(verified_result):
        resolved_prop_wcs = [backend.citation_for_ref(ref) for ref in prop.citations]
        if not resolved_prop_wcs or any(wc is None for wc in resolved_prop_wcs):
            continue
        prop_cites: list[dict[str, object]] = []
        seen_prop_tokens: set[str] = set()
        for wc in resolved_prop_wcs:
            assert wc is not None
            if wc.support_token not in seen_tokens:
                seen_tokens.add(wc.support_token)
                used_citations.append(wc)
            if wc.support_token not in seen_prop_tokens:
                seen_prop_tokens.add(wc.support_token)
                prop_cites.append(bench._saved_answer_citation_payload(wc))
        if prop_cites:
            proposals_payload.append({
                "proposal_id": prop.proposal_id, "category": prop.category, "label": prop.label,
                "title": prop.title, "detail": prop.detail, "date_label": prop.date_label,
                "calculation_ids": list(prop.calculation_ids), "disclosure_note": prop.disclosure_note,
                "citations": prop_cites,
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
            "receipts": receipt_rows, "gaps": verified_gaps,
            "competing_accounts": verified_competing,
            "proposals": proposals_payload,
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


def _verify_original_integrity(bench, store, doc_id: str) -> None:
    """Verify admitted original bytes for both uploaded and registered discovery sources."""
    doc = store.get(doc_id)
    if doc.origin == "registered":
        from pathlib import Path
        import tempfile
        from .source_locations import SourceScanItem

        item = SourceScanItem(
            doc.relative_path, Path(doc.relative_path).name, doc.media_type,
            doc.size, doc.stable_device, doc.stable_inode, doc.stable_mtime_ns,
        )
        with tempfile.TemporaryDirectory(prefix="ci-verify-") as tmp_dir:
            staged = bench.source_registry.stage(doc.source_location_id, item, Path(tmp_dir))
            if doc.digest and staged.sha256 != doc.digest:
                raise ValueError("Registered original changed on disk.")
    else:
        store.source_path(doc_id, verify_digest=True)


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
                    _verify_original_integrity(bench, store, doc_id)
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
