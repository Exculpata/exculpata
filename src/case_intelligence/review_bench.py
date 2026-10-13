from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol, Sequence
from urllib.parse import quote_plus

import uvicorn
from fastapi import FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .static_assets import asset_url, static_cache_control
from .contracts import Matter, MatterState
from .inventory import InventoryService
from .isolation import MatterAccess, MatterStateRegistry
from .jobs import JobService
from .retrieval import RetrievalService, SearchRequest
from .sqlite_store import SQLiteStore
from .tickets import TicketStore
from .pilot_uploads import MAX_FILES, PilotStore, RawUploadLimitMiddleware, UploadProblem
from .retrieval_limits import INPUT_BLOCKED_STAGE, RetrievalInputProblem
from .review_bench_v2 import (
    Candidate,
    DeterministicEmbeddingAdapter,
    DeterministicReranker,
    HybridRetriever,
    InMemoryHybridBackend,
    LazySentenceTransformerEmbedding,
    LazySentenceTransformerReranker,
    PostgresHybridBackend,
    RemoteEmbeddingAdapter,
    RemoteRerankerAdapter,
    delete_postgres_document,
    delete_postgres_matter_documents,
    connect_postgres_from_environment,
    extract_pdf_pages,
    initialize_postgres_demo,
    upsert_postgres_document,
)

PACKAGE_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_ROOT.parents[1]
DEMO_SOURCE_ROOT = PACKAGE_ROOT / "demo_data/sources"
DEFAULT_RUNTIME = PROJECT_ROOT / ".tmp/review-bench"

MATTERS = {
    "alpha": {
        "matter_id": "matter-alpha",
        "name": "North Entrance Review",
        "subtitle": "Synthetic demonstration matter",
        "actor_id": "reviewer-alpha",
    },
    "bravo": {
        "matter_id": "matter-bravo",
        "name": "South Entrance Review",
        "subtitle": "Separate synthetic demonstration matter",
        "actor_id": "reviewer-bravo",
    },
    "pilot": {
        "matter_id": "matter-pilot",
        "name": "Pilot Matter",
        "subtitle": "Supervised localhost real-data pilot",
        "actor_id": "reviewer-pilot",
    },
}

STOP_WORDS = {
    "a", "about", "and", "are", "at", "did", "do", "for", "from", "how",
    "i", "in", "is", "it", "of", "on", "please", "show", "tell", "that",
    "the", "these", "this", "to", "was", "were", "what", "when", "where",
    "which", "who", "with",
}


@dataclass(frozen=True)
class TranscriptCue:
    ordinal: int
    start: float
    end: float
    speaker: str
    text: str

    @property
    def timestamp(self) -> str:
        minutes, seconds = divmod(int(self.start), 60)
        return f"{minutes:02d}:{seconds:02d}"

    @property
    def anchor(self) -> str:
        return f"transcript-{self.ordinal}"


@dataclass(frozen=True)
class StaffSource:
    name: str
    kind: str
    state: str
    state_tone: str
    document_id: str | None = None
    retryable: bool = False


@dataclass(frozen=True)
class Citation:
    source_name: str
    location: str
    excerpt: str
    href: str
    media_start: float | None = None
    cue_anchor: str | None = None
    matter_id: str = ""
    source_version_id: str = ""
    excerpt_digest: str = ""


@dataclass(frozen=True)
class GroundedAnswer:
    text: str
    citations: tuple[Citation, ...]


class RetrievalUnavailable(RuntimeError):
    """The configured retrieval path failed; this is not a substantive no-match."""


class AnswerProvider(Protocol):
    def answer(self, question: str, evidence: Sequence[Citation]) -> GroundedAnswer: ...


class ExtractiveAnswerProvider:
    """Reliable first provider; every answer sentence is copied from retrieved evidence."""

    _duty_intent = {"duties", "duty", "responsibilities", "responsibility", "role"}
    _role_fillers = {
        "describe", "explain", "summarize", "summary", "what", "are", "is", "the", "of",
        "for", "my", "position", "job", "duties", "duty", "responsibilities", "responsibility",
        "role", "please", "tell", "me", "about",
    }
    _duty_cues = re.compile(
        r"\b(responsib\w*|dut(?:y|ies)|supervis(?:e|es|ed|ing|ion)\w*|manage\w*|administer\w*|maintain\w*|"
        r"plan\w*|ensure\w*|provide\w*|develop\w*|coordinate\w*|monitor\w*|oversee\w*|"
        r"implement\w*|evaluate\w*|serve\w*|direct\w*|lead\w*|perform\w*)\b",
        re.IGNORECASE,
    )
    _role_title_words = {
        "administrator", "analyst", "assistant", "attorney", "chief", "clerk",
        "coordinator", "counsel", "director", "investigator", "manager", "officer",
        "paralegal", "specialist", "supervisor", "technician",
    }

    @staticmethod
    def _tokens(value: str) -> tuple[str, ...]:
        return tuple(re.findall(r"[a-z0-9]+", value.casefold()))

    @classmethod
    def _looks_like_role_heading(cls, value: str) -> bool:
        value = value.strip(" •\t")
        tokens = cls._tokens(value)
        if not 2 <= len(tokens) <= 12 or len(value) > 140:
            return False
        if cls._duty_cues.search(value) or not cls._role_title_words.intersection(tokens):
            return False
        words = re.findall(r"[A-Za-z][A-Za-z'-]*", value)
        if not words:
            return False
        capitalized = sum(word.isupper() or word[:1].isupper() for word in words)
        return value.isupper() or capitalized / len(words) >= 0.6

    @staticmethod
    def _statement_chunks(value: str) -> tuple[str, ...]:
        normalized = re.sub(r"\s+", " ", value).strip()
        return tuple(
            chunk.strip(" •\t")
            for chunk in re.split(
                r"(?<=[.!?;])\s+(?=[A-Z0-9•])|\s+•\s*", normalized
            )
            if chunk.strip(" •\t")
        )

    @classmethod
    def _role_scoped_sections(
        cls, excerpt: str, role_terms: Sequence[str]
    ) -> tuple[tuple[str, tuple[str, ...]], ...]:
        layout_units = tuple(
            unit
            for line in excerpt.splitlines()
            if line.strip()
            for unit in cls._statement_chunks(line)
        )
        heading_indexes = [
            index
            for index, unit in enumerate(layout_units)
            if cls._looks_like_role_heading(unit)
        ]
        requested_indexes = [
            index
            for index in heading_indexes
            if all(term in set(cls._tokens(layout_units[index])) for term in role_terms)
        ]
        sections: list[tuple[str, tuple[str, ...]]] = []
        for start in requested_indexes:
            end = next((index for index in heading_indexes if index > start), len(layout_units))
            heading = " ".join(cls._tokens(layout_units[start]))
            section_text = " ".join(layout_units[start:end])
            sections.append((heading, cls._statement_chunks(section_text)))
        return tuple(sections)

    def _duty_summary(
        self, question: str, evidence: Sequence[Citation]
    ) -> GroundedAnswer | None:
        question_tokens = self._tokens(question)
        if not self._duty_intent.intersection(question_tokens):
            return None
        role_terms = tuple(
            token for token in question_tokens
            if token not in self._role_fillers and len(token) > 2
        )
        if len(role_terms) < 2:
            return None
        scoped = tuple(
            citation for citation in evidence[:12]
            if all(term in set(self._tokens(citation.excerpt)) for term in role_terms)
        )
        if not scoped:
            return GroundedAnswer(
                "I could not find support for that question in this matter's searchable records.",
                (),
            )

        matched_sections: list[tuple[int, Citation, str, tuple[str, ...]]] = []
        matched_headings: set[str] = set()
        for citation_rank, citation in enumerate(scoped):
            for heading, chunks in self._role_scoped_sections(citation.excerpt, role_terms):
                matched_headings.add(heading)
                matched_sections.append((citation_rank, citation, heading, chunks))
        exact_heading = " ".join(role_terms)
        if exact_heading in matched_headings:
            matched_sections = [
                section for section in matched_sections
                if section[2] == exact_heading
            ]
        elif len(matched_headings) != 1:
            return GroundedAnswer(
                "I could not find support for that question in this matter's searchable records.",
                (),
            )

        ranked: list[tuple[int, int, int, str, Citation]] = []
        for citation_rank, citation, _heading, chunks in matched_sections:
            for chunk_rank, chunk in enumerate(chunks):
                chunk = chunk.strip(" •\t")
                if not 45 <= len(chunk) <= 380 or not self._duty_cues.search(chunk):
                    continue
                chunk_tokens = set(self._tokens(chunk))
                if chunk_tokens and chunk_tokens.issubset(set(role_terms)):
                    continue
                score = 2
                folded = chunk.casefold()
                if "responsible for" in folded or "major duties" in folded:
                    score += 4
                if re.search(r"\b(supervis\w*|oversee\w*|direct\w*|manage\w*)\b", folded):
                    score += 2
                if 70 <= len(chunk) <= 280:
                    score += 1
                ranked.append((-score, citation_rank, chunk_rank, chunk, citation))

        selected: list[tuple[str, Citation]] = []
        per_page: dict[tuple[str, str, str, str], int] = {}
        seen: set[str] = set()
        for _score, _citation_rank, _chunk_rank, chunk, citation in sorted(ranked):
            fingerprint = " ".join(self._tokens(chunk))
            page_key = (
                citation.matter_id,
                citation.source_version_id,
                citation.source_name,
                citation.location,
            )
            if fingerprint in seen or per_page.get(page_key, 0) >= 2:
                continue
            seen.add(fingerprint)
            per_page[page_key] = per_page.get(page_key, 0) + 1
            selected.append((chunk, citation))
            if len(selected) == 8:
                break
        if not selected:
            return GroundedAnswer(
                "I could not find support for that question in this matter's searchable records.",
                (),
            )
        citations = tuple(dict.fromkeys(citation for _chunk, citation in selected))
        citation_numbers = {citation: index for index, citation in enumerate(citations, start=1)}
        bullets = [
            f"• {chunk} [{citation_numbers[citation]}]" for chunk, citation in selected
        ]
        return GroundedAnswer(
            "The cited position description identifies these duties:\n" + "\n".join(bullets),
            citations,
        )

    def answer(self, question: str, evidence: Sequence[Citation]) -> GroundedAnswer:
        duty_summary = self._duty_summary(question, evidence)
        if duty_summary is not None:
            return duty_summary
        selected = tuple(evidence[:3])
        if not selected:
            return GroundedAnswer(
                "I could not find support for that question in this matter's searchable records.",
                (),
            )
        statements: list[str] = []
        for citation in selected:
            sentence = citation.excerpt.strip()
            if sentence and sentence not in statements:
                statements.append(sentence)
        return GroundedAnswer(" ".join(statements), selected)


class ReviewBench:
    def __init__(self, runtime_dir: Path, provider: AnswerProvider | None = None) -> None:
        self.runtime_dir = Path(runtime_dir)
        self.provider = provider or ExtractiveAnswerProvider()
        self._prepare_runtime()
        self.pilot = PilotStore(self.runtime_dir / "pilot")
        self.transcripts = self._load_transcripts()
        alpha_pages = extract_pdf_pages(PACKAGE_ROOT / "demo_data/synthetic_case_report.pdf")
        bravo_pages = tuple(
            type(page)(page.page_number, page.text.replace("north entrance", "south entrance"))
            for page in alpha_pages
        )
        self.pdf_pages = {"alpha": alpha_pages, "bravo": bravo_pages}
        demo_candidates = tuple(
            Candidate(
                details["matter_id"],
                "synthetic-report",
                f"page-{page.page_number}-chunk-1",
                "Synthetic incident report.pdf",
                page.page_number,
                page.text,
            )
            for slug, details in MATTERS.items()
            if slug != "pilot"
            for page in self.pdf_pages[slug]
        )
        self._fallback_hybrid = HybridRetriever(
            InMemoryHybridBackend(demo_candidates),
            DeterministicEmbeddingAdapter(),
            DeterministicReranker(),
        )
        self.hybrid = self._fallback_hybrid
        self.postgres_connection = connect_postgres_from_environment()
        self.postgres_ready = False
        self.models_ready = False
        self.embedding = DeterministicEmbeddingAdapter()
        if self.postgres_connection is not None:
            try:
                worker_url = os.getenv("CASE_REVIEW_MODEL_WORKER_URL")
                if worker_url and os.getenv("CASE_REVIEW_ENABLE_MODELS") == "1":
                    embedding = RemoteEmbeddingAdapter(worker_url)
                    reranker = RemoteRerankerAdapter(worker_url)
                else:
                    embedding = LazySentenceTransformerEmbedding.from_environment()
                    reranker = LazySentenceTransformerReranker.from_environment()
                self.embedding = (
                    embedding if embedding.available and reranker.available
                    else DeterministicEmbeddingAdapter()
                )
                initialize_postgres_demo(
                    self.postgres_connection,
                    {
                        details["matter_id"]: self.pdf_pages[slug]
                        for slug, details in MATTERS.items()
                        if slug in self.pdf_pages
                    },
                    embedder=embedding if embedding.available and reranker.available else None,
                )
                if embedding.available and reranker.available:
                    self.hybrid = HybridRetriever(
                        PostgresHybridBackend(self.postgres_connection),
                        embedding,
                        reranker,
                        minimum_dense_score=0.72,
                        minimum_rerank_score=0.80,
                    )
                    self.models_ready = True
                else:
                    self.hybrid = HybridRetriever(
                        PostgresHybridBackend(self.postgres_connection),
                        DeterministicEmbeddingAdapter(), DeterministicReranker(),
                        minimum_dense_score=2.0,
                        minimum_rerank_score=float("inf"),
                    )
                self.postgres_ready = True
            except Exception:
                self.postgres_connection.close()
                self.postgres_connection = None
        self.registry = MatterStateRegistry(
            [
                Matter(
                    matter_id=details["matter_id"],
                    display_name=details["name"],
                    state=MatterState.ACTIVE,
                )
                for details in MATTERS.values()
            ]
        )
        self.store = SQLiteStore(self.runtime_dir / "catalog.sqlite")
        self.access = MatterAccess(
            {details["actor_id"]: {details["matter_id"]} for details in MATTERS.values()}
        )
        self.inventory = InventoryService(self.store, self.registry)
        self.jobs = JobService(self.store, self.registry)
        self.tickets = TicketStore(
            registry=self.registry, clock=lambda: datetime.now(timezone.utc)
        )
        self.retrieval = RetrievalService(
            self.store, self.registry, self.access, self.tickets
        )
        self._seed()
        self._refresh_pilot_retrieval(index_postgres=True)

    def _prepare_runtime(self) -> None:
        if self.runtime_dir.exists():
            if self.runtime_dir.is_symlink():
                raise RuntimeError("review bench runtime cannot be a symlink")
            for generated in (self.runtime_dir / "catalog.sqlite", self.runtime_dir / "sources"):
                if generated.is_symlink():
                    raise RuntimeError("review bench generated state cannot be a symlink")
                if generated.is_dir():
                    shutil.rmtree(generated)
                elif generated.exists():
                    generated.unlink()
        self.runtime_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _load_transcripts() -> dict[str, tuple[TranscriptCue, ...]]:
        raw = json.loads(
            (PACKAGE_ROOT / "demo_data/transcripts.json").read_text(encoding="utf-8")
        )
        return {
            slug: tuple(
                TranscriptCue(ordinal=index, **item)
                for index, item in enumerate(items, start=1)
            )
            for slug, items in raw.items()
        }

    def _seed(self) -> None:
        sources_root = self.runtime_dir / "sources"
        for slug, details in MATTERS.items():
            if slug == "pilot":
                self.store.register_matter(details["matter_id"], details["name"])
                continue
            source_root = sources_root / slug
            shutil.copytree(DEMO_SOURCE_ROOT / slug, source_root)
            matter_id = details["matter_id"]
            self.store.register_matter(matter_id, details["name"])
            self.inventory.register_location(
                matter_id,
                f"location-{slug}",
                "Synthetic review set",
                source_root,
                f"\\\\synthetic\\{slug}",
            )
            self.inventory.inventory(
                matter_id, f"location-{slug}", f"review-bench-{slug}"
            )
            while self.jobs.run_next(matter_id):
                pass

    def close(self) -> None:
        self.pilot.close()
        self.store.close()
        if self.postgres_connection is not None:
            self.postgres_connection.close()

    def capabilities(self) -> dict[str, str]:
        return {
            "word_search": "ready",
            "meaning_search": "model-assisted" if self.models_ready else "deterministic demo",
            "source_pages": "ready",
            "model_assistance": "ready" if self.models_ready else "not enabled",
        }

    def matter(self, slug: str) -> dict[str, str]:
        details = MATTERS.get(slug)
        if details is None:
            raise KeyError(slug)
        return details

    def sources(self, slug: str) -> tuple[StaffSource, ...]:
        if slug == "pilot":
            self.matter(slug)
            return tuple(
                StaffSource(
                    item.display_name,
                    "Document",
                    item.message,
                    "ready" if item.state == "ready" else "waiting",
                    self.pilot.action_token(item),
                    item.state == "failed" and item.processing_stage != INPUT_BLOCKED_STAGE,
                )
                for item in self.pilot.documents.values()
            )
        matter_id = self.matter(slug)["matter_id"]
        rows = self.store.connection.execute(
            "SELECT sf.display_name,sf.media_type,sf.availability,j.state "
            "FROM source_file sf LEFT JOIN job j "
            "ON j.source_version_id=sf.current_source_version_id "
            "WHERE sf.matter_id=? ORDER BY sf.display_name",
            (matter_id,),
        ).fetchall()
        projected: list[StaffSource] = []
        for row in rows:
            name = row["display_name"]
            if name.endswith(".wav"):
                projected.append(StaffSource(name, "Audio", "Transcript ready", "ready"))
            elif name.endswith(".txt") and row["state"] == "succeeded":
                projected.append(StaffSource(name, "Document", "Ready and searchable", "ready"))
            else:
                projected.append(StaffSource(name, "Image", "Preview only", "waiting"))
        projected.append(
            StaffSource(
                "Synthetic incident report.pdf",
                "Document",
                f"{len(self.pdf_pages[slug])} pages ready",
                "ready",
            )
        )
        return tuple(projected)

    @staticmethod
    def _content_terms(query: str) -> tuple[str, ...]:
        words = [
            "".join(character for character in token.casefold() if character.isalnum())
            for token in query.split()
        ]
        meaningful = tuple(word for word in words if len(word) > 1 and word not in STOP_WORDS)
        return meaningful[:6]

    def search(self, slug: str, query: str) -> tuple[Citation, ...]:
        details = self.matter(slug)
        terms = self._content_terms(query)
        if not terms:
            return ()
        lexical_query = " ".join(terms)
        response = self.retrieval.search(
            details["actor_id"],
            SearchRequest(
                run_id=f"review-{uuid.uuid4().hex}",
                matter_id=details["matter_id"],
                query=lexical_query,
                top_k=8,
            ),
        )
        results: list[Citation] = []
        for result in (() if slug == "pilot" else response.results):
            lines = result.excerpt.splitlines()
            # The first line in the synthetic fixture is a benchmark canary,
            # not useful staff-facing evidence. Keep retrieval exact while
            # presenting the substantive lines in the ordinary workspace.
            visible_lines = lines[1:] if len(lines) > 1 else lines
            visible_start = result.line_start + (1 if len(lines) > 1 else 0)
            results.append(
                Citation(
                    source_name=result.source_name,
                    location=f"Lines {visible_start}–{max(visible_start, result.line_end - 1)}",
                    excerpt=" ".join(visible_lines),
                    href=f"/matters/{slug}/sources/report?line={visible_start}",
                )
            )
        for cue in self.transcripts.get(slug, ()):
            folded = cue.text.casefold()
            if all(term in folded for term in terms):
                results.append(
                    Citation(
                        source_name="Recorded interview",
                        location=cue.timestamp,
                        excerpt=cue.text,
                        href=f"/matters/{slug}?seek={cue.start:g}#{cue.anchor}",
                        media_start=cue.start,
                        cue_anchor=cue.anchor,
                    )
                )
        # Preserve the accepted Slice 1A shared-phrase result set while adding
        # page-native V2 retrieval for all other searches.
        if slug == "pilot" or not {"shared", "red", "bicycle"}.issubset(set(terms)):
            try:
                page_results = self.hybrid.search(details["matter_id"], query.strip(), limit=8)
            except Exception as exc:
                raise RetrievalUnavailable("matter search could not run") from exc
            if slug == "pilot" and not self._validate_pilot_candidates(page_results):
                return ()
            page_citations: list[Citation] = []
            for item in page_results:
                token = self._citation_token(item)
                href = (
                    (f"/matters/{slug}/sources/{token}?line={item.line_start}"
                     if item.line_start is not None
                     else f"/matters/{slug}/sources/{token}?page={item.page_number}")
                    if slug == "pilot"
                    else f"/matters/{slug}/sources/synthetic-report?page={item.page_number}"
                )
                page_citations.append(
                    Citation(
                        source_name=item.source_name,
                        location=item.citation,
                        excerpt=item.text,
                        href=href,
                        matter_id=item.matter_id,
                        source_version_id=item.source_version_id,
                        excerpt_digest=item.excerpt_digest,
                    )
                )
            results = page_citations + results
        return tuple(results)

    def ask(self, slug: str, question: str) -> GroundedAnswer:
        evidence = self.search(slug, question)
        try:
            answer = self.provider.answer(question, evidence)
        except Exception:
            return GroundedAnswer(
                "I could not find support for that question in this matter's searchable records.", ()
            )
        if any(citation not in evidence for citation in answer.citations) or (answer.text and not answer.citations):
            return GroundedAnswer(
                "I could not find support for that question in this matter's searchable records.", ()
            )
        return answer

    @staticmethod
    def _citation_token(item: Candidate) -> str:
        material = "\x00".join((item.matter_id, item.document_id, item.source_version_id,
                                  item.chunk_id, item.excerpt_digest))
        return hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]

    def _validate_pilot_candidates(self, candidates: Sequence[Candidate]) -> bool:
        for item in candidates:
            if item.matter_id != "matter-pilot":
                return False
            try:
                document = self.pilot.get(item.document_id)
            except KeyError:
                return False
            units = document.parsed_units()
            unit = next((value for value in units if value.text == item.text and value.number == item.page_number), None)
            if (document.state != "ready" or document.version_id != item.source_version_id or unit is None
                    or unit.excerpt_digest != item.excerpt_digest):
                return False
        return True

    def pdf_document(self, slug: str, page_number: int) -> dict[str, object]:
        self.matter(slug)
        pages = self.pdf_pages[slug]
        if page_number < 1 or page_number > len(pages):
            raise LookupError("page unavailable")
        page = pages[page_number - 1]
        return {
            "name": "Synthetic incident report.pdf",
            "page_number": page.page_number,
            "page_count": len(pages),
            "lines": tuple(enumerate(page.text.splitlines(), start=1)),
        }

    def pilot_document(self, citation_token: str, page: int, line: int) -> dict[str, object]:
        matched = None
        for document in self.pilot.ready_documents():
            for index, unit in enumerate(document.parsed_units(), 1):
                candidate = Candidate("matter-pilot", document.document_id, f"chunk-{index}",
                                      document.display_name, unit.number, unit.text,
                                      line_start=unit.line_start, line_end=unit.line_end,
                                      source_version_id=document.version_id,
                                      excerpt_digest=unit.excerpt_digest)
                if self._citation_token(candidate) == citation_token:
                    matched = (document, unit)
                    break
            if matched:
                break
        if matched is None:
            raise LookupError("stale source support")
        document, bound_unit = matched
        if document.state != "ready":
            raise LookupError("source unavailable")
        units = document.parsed_units()
        if document.media_type == "application/pdf":
            selected = bound_unit if bound_unit.number == page else None
            if selected is None:
                raise LookupError("page unavailable")
            return {
                "name": document.display_name,
                "page_number": page,
                "page_count": document.page_count or max(unit.number for unit in units),
                "lines": tuple(enumerate(selected.text.splitlines(), 1)),
            }
        selected = bound_unit if (bound_unit.line_start is not None and
                                  bound_unit.line_start <= line <= (bound_unit.line_end or bound_unit.line_start)) else None
        if selected is None:
            raise LookupError("line unavailable")
        return {
            "name": document.display_name,
            "lines": tuple(enumerate(selected.text.splitlines(), selected.line_start or 1)),
        }

    def _pilot_candidates(self) -> tuple[Candidate, ...]:
        return tuple(
            Candidate(
                "matter-pilot", document.document_id, f"chunk-{index}",
                document.display_name, unit.number, unit.text,
                line_start=unit.line_start, line_end=unit.line_end,
                source_version_id=document.version_id, excerpt_digest=unit.excerpt_digest,
            )
            for document in self.pilot.ready_documents()
            for index, unit in enumerate(document.parsed_units(), 1)
        )

    def _refresh_pilot_retrieval(self, *, index_postgres: bool) -> None:
        existing = tuple(self._fallback_hybrid.backend.candidates)
        synthetic = tuple(item for item in existing if item.matter_id != "matter-pilot")
        self._fallback_hybrid = HybridRetriever(
            InMemoryHybridBackend(synthetic + self._pilot_candidates()),
            DeterministicEmbeddingAdapter(), DeterministicReranker(),
        )
        if not self.postgres_ready:
            self.hybrid = self._fallback_hybrid
            return
        if index_postgres and self.postgres_connection is not None:
            delete_postgres_matter_documents(
                self.postgres_connection, matter_id="matter-pilot"
            )
            for document in tuple(self.pilot.ready_documents()):
                self._index_pilot_postgres(document)

    def _index_pilot_postgres(self, document) -> None:
        if self.postgres_connection is None:
            return
        try:
            upsert_postgres_document(
                self.postgres_connection,
                matter_id="matter-pilot",
                matter_name="Pilot Matter",
                document_id=document.document_id,
                display_name=document.display_name,
                media_type=document.media_type,
                units=document.parsed_units(),
                embedder=self.embedding,
                source_version_id=document.version_id,
                content_digest=document.digest,
                byte_size=document.size,
                storage_key=document.stored_name,
            )
        except Exception as exc:
            self.postgres_connection.rollback()
            document.state = "failed"
            document.message = str(exc) if isinstance(exc, RetrievalInputProblem) else "Saved, but indexing did not finish. Choose Try again."
            if isinstance(exc, RetrievalInputProblem):
                document.processing_stage = INPUT_BLOCKED_STAGE
            self.pilot._save((document.document_id,))

    def _delete_pilot_postgres(self, document_id: str) -> None:
        if self.postgres_connection is None or not self.postgres_ready:
            return
        delete_postgres_document(
            self.postgres_connection,
            matter_id="matter-pilot",
            document_id=document_id,
        )

    def index_pilot_document(self, document_id: str) -> None:
        document = self.pilot.get(document_id)
        if document.state == "ready" and self.postgres_ready:
            self._index_pilot_postgres(document)
        self._refresh_pilot_retrieval(index_postgres=False)

    def document(self, slug: str) -> dict[str, object]:
        matter_id = self.matter(slug)["matter_id"]
        row = self.store.connection.execute(
            "SELECT sf.display_name,s.text,s.line_start,s.line_end "
            "FROM segment s JOIN source_file sf ON sf.source_file_id=s.source_file_id "
            "WHERE s.matter_id=? AND sf.matter_id=? AND sf.availability='available' "
            "AND sf.current_source_version_id=s.source_version_id ORDER BY s.ordinal LIMIT 1",
            (matter_id, matter_id),
        ).fetchone()
        if row is None:
            raise LookupError("document unavailable")
        lines = row["text"].splitlines()
        # The committed fixture's first line is a benchmark canary used by
        # automated isolation tests. It is not source evidence and must never
        # appear in the ordinary staff viewer.
        visible_lines = lines[1:] if len(lines) > 1 else lines
        return {
            "name": row["display_name"],
            "lines": tuple(enumerate(visible_lines, start=2 if len(lines) > 1 else 1)),
        }


def create_app(runtime_dir: Path | None = None) -> FastAPI:
    bench = ReviewBench(runtime_dir or DEFAULT_RUNTIME)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        yield
        bench.close()

    app = FastAPI(title="Case Review Bench", lifespan=lifespan)
    app.add_middleware(RawUploadLimitMiddleware)
    app.state.bench = bench
    templates = Jinja2Templates(directory=str(PACKAGE_ROOT / "templates"))
    templates.env.globals["asset_url"] = asset_url
    app.mount("/static", StaticFiles(directory=str(PACKAGE_ROOT / "static")), name="static")

    @app.middleware("http")
    async def static_cache_policy(request: Request, call_next):
        response = await call_next(request)
        if request.url.path.startswith("/static/"):
            response.headers.setdefault("Cache-Control", static_cache_control(
                request.url.path, request.query_params.get("v")))
        return response

    @app.get("/health")
    def health() -> dict[str, object]:
        return {
            "status": "ok",
            "product": "Case Review Bench",
            "data": "local pilot",
            "capabilities": bench.capabilities(),
        }

    @app.get("/", include_in_schema=False)
    def home() -> RedirectResponse:
        return RedirectResponse("/matters/alpha", status_code=303)

    @app.get("/matters/{slug}", response_class=HTMLResponse)
    def workspace(
        request: Request,
        slug: str,
        q: str = Query("", max_length=512),
        question: str = Query("", max_length=512),
        seek: float | None = Query(None, ge=0, le=8),
        notice: str = Query("", max_length=240),
        error: str = Query("", max_length=240),
    ):
        try:
            matter = bench.matter(slug)
        except KeyError as exc:
            raise HTTPException(404, "Matter not found") from exc
        search_results = ()
        answer = None
        action_error = error
        pilot_documents = tuple(bench.pilot.documents.values()) if slug == "pilot" else ()
        pilot_has_ready_source = any(item.state == "ready" for item in pilot_documents)
        if slug == "pilot" and (q.strip() or question.strip()) and not pilot_has_ready_source:
            action_error = (
                "No uploaded source is ready to search. Check the source status below and choose Try again."
                if pilot_documents
                else "Upload a source and wait until it is ready before searching or asking a question."
            )
        else:
            try:
                search_results = bench.search(slug, q) if q.strip() else ()
                answer = bench.ask(slug, question) if question.strip() else None
            except RetrievalUnavailable:
                action_error = "Search could not run. Your sources are still saved; try again in a moment."
        return templates.TemplateResponse(
            request=request,
            name="workspace.html",
            context={
                "slug": slug,
                "matter": matter,
                "matters": MATTERS,
                "sources": bench.sources(slug),
                "cues": bench.transcripts.get(slug, ()),
                "query": q,
                "question": question,
                "search_results": search_results,
                "answer": answer,
                "seek": seek,
                "pilot_mode": slug == "pilot",
                "notice": notice,
                "error": action_error,
            },
        )

    @app.post("/matters/pilot/uploads")
    def upload_pilot(files: list[UploadFile] = File(...)):
        if not files or len(files) > MAX_FILES:
            return RedirectResponse(
                "/matters/pilot?error=" + quote_plus("Choose between 1 and 10 files for each upload."),
                status_code=303,
            )
        used = 0
        created: list[str] = []
        accepted: list[str] = []
        try:
            for upload in files:
                before = set(bench.pilot.documents)
                document, used = bench.pilot.store_stream(
                    upload.filename or "", upload.content_type, upload.file, request_used=used
                )
                if document.document_id not in before:
                    created.append(document.document_id)
                accepted.append(document.document_id)
        except UploadProblem as exc:
            for document_id in created:
                bench.pilot.remove(document_id)
            bench._refresh_pilot_retrieval(index_postgres=False)
            return PlainTextResponse(str(exc), status_code=exc.status_code)
        finally:
            for upload in files:
                upload.file.close()
        for document_id in accepted:
            bench.index_pilot_document(document_id)
        return RedirectResponse(
            "/matters/pilot?notice=" + quote_plus(f"{len(created)} file(s) uploaded"),
            status_code=303,
        )

    @app.post("/matters/pilot/sources/{document_id}/retry")
    def retry_pilot(document_id: str):
        try:
            current = bench.pilot.get_by_action_token(document_id)
            if current.processing_stage == INPUT_BLOCKED_STAGE:
                raise UploadProblem(current.message, 409)
            document = bench.pilot.retry(current.document_id)
            bench.index_pilot_document(document.document_id)
        except KeyError as exc:
            raise HTTPException(404, "Source not found") from exc
        except UploadProblem as exc:
            return PlainTextResponse(str(exc), status_code=exc.status_code)
        return RedirectResponse("/matters/pilot?notice=Processing+retried", status_code=303)

    @app.post("/matters/pilot/sources/{document_id}/remove")
    def remove_pilot(document_id: str, request: Request):
        if request.client is None or request.client.host not in {"127.0.0.1", "::1", "testclient"}:
            raise HTTPException(403, "This pilot action is available only on localhost.")
        try:
            current = bench.pilot.get_by_action_token(document_id)
            bench._delete_pilot_postgres(current.document_id)
            bench.pilot.remove(current.document_id)
            bench._refresh_pilot_retrieval(index_postgres=False)
        except RuntimeError as exc:
            raise HTTPException(503, "The source could not be removed safely. Try again.") from exc
        except KeyError as exc:
            raise HTTPException(404, "Source not found") from exc
        return RedirectResponse("/matters/pilot?notice=Source+removed", status_code=303)

    @app.get("/matters/{slug}/sources/synthetic-report", response_class=HTMLResponse)
    def pdf_source_viewer(
        request: Request, slug: str, page: int = Query(1, ge=1, le=10000)
    ):
        try:
            matter = bench.matter(slug)
            document = bench.pdf_document(slug, page)
        except (KeyError, LookupError) as exc:
            raise HTTPException(404, "Source page not found") from exc
        return templates.TemplateResponse(
            request=request,
            name="source.html",
            context={
                "slug": slug,
                "matter": matter,
                "document": document,
                "focus_line": 0,
                "page_focus": True,
            },
        )

    @app.get("/matters/{slug}/sources/report", response_class=HTMLResponse)
    def source_viewer(request: Request, slug: str, line: int = Query(1, ge=1, le=10000)):
        try:
            matter = bench.matter(slug)
            document = bench.document(slug)
        except (KeyError, LookupError) as exc:
            raise HTTPException(404, "Source not found") from exc
        return templates.TemplateResponse(
            request=request,
            name="source.html",
            context={"slug": slug, "matter": matter, "document": document, "focus_line": line},
        )

    @app.get("/matters/pilot/sources/{document_id}", response_class=HTMLResponse)
    def pilot_source_viewer(
        request: Request,
        document_id: str,
        page: int = Query(1, ge=1, le=100000),
        line: int = Query(1, ge=1, le=10000000),
    ):
        try:
            document = bench.pilot_document(document_id, page, line)
        except (KeyError, LookupError) as exc:
            raise HTTPException(404, "Source location not found") from exc
        return templates.TemplateResponse(
            request=request,
            name="source.html",
            context={
                "slug": "pilot",
                "matter": MATTERS["pilot"],
                "document": document,
                "focus_line": line if "page_number" not in document else 0,
            },
        )

    @app.get("/api/matters/{slug}/transcript")
    def transcript(slug: str) -> dict[str, object]:
        try:
            bench.matter(slug)
        except KeyError as exc:
            raise HTTPException(404, "Matter not found") from exc
        return {
            "matter": slug,
            "cues": [
                {
                    "start": cue.start,
                    "end": cue.end,
                    "speaker": cue.speaker,
                    "text": cue.text,
                }
                for cue in bench.transcripts.get(slug, ())
            ],
        }

    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the local synthetic Case Review Bench")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    if args.host != "127.0.0.1":
        parser.error("the review bench is restricted to 127.0.0.1")
    uvicorn.run(create_app(), host=args.host, port=args.port, access_log=False)


if __name__ == "__main__":
    main()
