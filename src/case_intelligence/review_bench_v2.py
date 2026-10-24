"""Development-only native PDF and hybrid retrieval seams for Review Bench V2."""

from __future__ import annotations

from .pdf_extract_helper import HELPER_STARTUP_ERROR
from .review_budget import validate_primary_limit

import hashlib
import math
import os
import re
import json
import subprocess
import sys
from .service_endpoints import open_service_request
import urllib.request
import urllib.error
from contextlib import nullcontext
from dataclasses import dataclass, replace
from importlib import resources
from pathlib import Path
from typing import Callable, Iterable, Mapping, Protocol, Sequence

from .review_quality import rank_exact_record_candidates, rank_temporal_candidates
from .retrieval_limits import (
    MAX_EMBEDDING_TEXTS, MAX_RERANK_TEXTS, RetrievalInputProblem, request_batches, text_fragments,
)

DEFAULT_EMBEDDING_MODEL = "ibm-granite/granite-embedding-english-r2"
DEFAULT_RERANKER_MODEL = "Alibaba-NLP/gte-reranker-modernbert-base"


def _in_process_device() -> dict[str, str]:
    """Pass an explicit CPU or GPU device. Unset keeps the library default and stays in-process."""

    device = os.getenv("CASE_REVIEW_MODEL_DEVICE", "").strip()
    return {"device": device} if device else {}
_TOKEN = re.compile(r"[a-z0-9]+")
_WORKBENCH_MATTER_ID = re.compile(r"^ci-matter-[0-9a-f]{32}$")
_MEDIA_SUFFIXES = {".wav", ".mp3", ".m4a", ".ogg", ".opus", ".mp4", ".mov", ".webm"}


def _media_timestamp(milliseconds: int) -> str:
    hours, remainder = divmod(max(int(milliseconds), 0), 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds = remainder // 1_000
    return (
        f"{hours:02d}:{minutes:02d}:{seconds:02d}"
        if hours
        else f"{minutes:02d}:{seconds:02d}"
    )


@dataclass(frozen=True)
class PdfPage:
    page_number: int
    text: str
    image_coverage: float = 0.0
    image_evidence_known: bool = False


@dataclass(frozen=True)
class Candidate:
    matter_id: str
    document_id: str
    chunk_id: str
    source_name: str
    page_number: int
    text: str
    score: float = 0.0
    line_start: int | None = None
    line_end: int | None = None
    source_version_id: str = "synthetic-v1"
    excerpt_digest: str = ""
    evidence_kind: str = "document"

    @property
    def citation(self) -> str:
        if Path(self.source_name).suffix.casefold() in _MEDIA_SUFFIXES and self.line_start is not None:
            end = self.line_end if self.line_end is not None else self.line_start
            return f"{_media_timestamp(self.line_start)}–{_media_timestamp(end)}"
        if self.line_start is not None:
            end = self.line_end or self.line_start
            return f"Lines {self.line_start}–{end}" if end != self.line_start else f"Line {self.line_start}"
        if self.source_name.casefold().endswith(".docx"):
            return f"Section {self.page_number}"
        return f"Page {self.page_number}"


class EmbeddingAdapter(Protocol):
    @property
    def available(self) -> bool: ...

    def embed(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]: ...


class RerankerAdapter(Protocol):
    @property
    def available(self) -> bool: ...

    def rerank(self, query: str, candidates: Sequence[Candidate]) -> tuple[Candidate, ...]: ...


class HybridBackend(Protocol):
    def lexical(self, matter_id: str, query: str, limit: int) -> Sequence[Candidate]: ...

    def dense(
        self, matter_id: str, query_vector: Sequence[float], query: str, limit: int
    ) -> Sequence[Candidate]: ...


def extract_pdf_pages(path: Path) -> tuple[PdfPage, ...]:
    """Extract PDF text in a resource-limited, short-lived child process."""
    source = Path(path)
    if not source.is_file() or source.is_symlink():
        raise ValueError("PDF source must be a regular, non-symlink file")
    try:
        completed = subprocess.run(
            [
                sys.executable,
                str(Path(__file__).with_name("pdf_extract_helper.py")),
                str(source),
            ],
            capture_output=True,
            check=False,
            timeout=15,
        )
    except subprocess.TimeoutExpired as exc:
        raise ValueError("PDF extraction timed out") from exc
    if len(completed.stdout) > 16 * 1024 * 1024:
        raise ValueError("PDF extraction output exceeded its limit")
    try:
        payload = json.loads(completed.stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        if completed.returncode != 0:
            raise ValueError("PDF extraction could not start.") from exc
        raise ValueError("PDF could not be extracted") from exc
    if not isinstance(payload, dict):
        raise ValueError("PDF extraction returned invalid output")
    error = payload.get("error")
    if error == "encrypted":
        raise ValueError("Encrypted PDFs are not accepted in this pilot.")
    if error in {"page_limit", "page_text_limit", "total_text_limit", "chunk_limit"}:
        raise ValueError("That PDF exceeds the pilot extraction limits.")
    if error == HELPER_STARTUP_ERROR or (completed.returncode != 0 and not error):
        raise ValueError("PDF extraction could not start.")
    if completed.returncode != 0 or error:
        raise ValueError("That PDF is damaged or malformed.")
    pages = payload.get("pages")
    if not isinstance(pages, list) or len(pages) > 500:
        raise ValueError("PDF extraction returned invalid output")
    result: list[PdfPage] = []
    total = 0
    for index, item in enumerate(pages, 1):
        if (not isinstance(item, dict) or type(item.get("page_number")) is not int
                or item["page_number"] != index or not isinstance(item.get("text"), str)):
            raise ValueError("PDF extraction returned invalid output")
        image_coverage = item.get("image_coverage")
        image_evidence_known = item.get("image_evidence_known")
        if (type(image_coverage) not in (int, float) or not 0 <= image_coverage <= 1
                or not math.isfinite(image_coverage) or type(image_evidence_known) is not bool):
            raise ValueError("PDF extraction returned invalid image evidence")
        text = item["text"]
        if len(text) > 250_000:
            raise ValueError("That PDF exceeds the pilot extraction limits.")
        total += len(text)
        if total > 5_000_000:
            raise ValueError("That PDF exceeds the pilot extraction limits.")
        result.append(PdfPage(index, text, float(image_coverage), image_evidence_known))
    return tuple(result)


def _tokens(text: str) -> tuple[str, ...]:
    return tuple(_TOKEN.findall(text.casefold()))


class DeterministicEmbeddingAdapter:
    """Small, reproducible test/demo adapter; not a learned semantic model."""

    dimensions = 768
    available = True

    def embed(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]:
        vectors: list[tuple[float, ...]] = []
        for text in texts:
            values = [0.0] * self.dimensions
            for token in _tokens(text):
                digest = hashlib.blake2s(token.encode(), digest_size=4).digest()
                slot = int.from_bytes(digest, "big") % self.dimensions
                values[slot] += 1.0
            norm = math.sqrt(sum(value * value for value in values)) or 1.0
            vectors.append(tuple(value / norm for value in values))
        return tuple(vectors)


class DeterministicReranker:
    """Stable overlap reranker used by core tests and the no-dependency demo."""

    available = True

    def rerank(self, query: str, candidates: Sequence[Candidate]) -> tuple[Candidate, ...]:
        query_terms = set(_tokens(query))
        scored = []
        for candidate in candidates:
            overlap = len(query_terms.intersection(_tokens(candidate.text)))
            scored.append(replace(candidate, score=float(overlap * 100) + candidate.score))
        return tuple(sorted(scored, key=lambda item: (-item.score, item.page_number, item.chunk_id)))


class LazySentenceTransformerEmbedding:
    """Environment-gated SentenceTransformers adapter imported only on first use."""

    def __init__(self, model_name: str, enabled: bool, local_only: bool = True) -> None:
        self._model_name = model_name
        self._enabled = enabled
        self._local_only = local_only
        self._model = None

    @classmethod
    def from_environment(cls) -> "LazySentenceTransformerEmbedding":
        return cls(
            os.getenv("CASE_REVIEW_EMBEDDING_MODEL", DEFAULT_EMBEDDING_MODEL),
            os.getenv("CASE_REVIEW_ENABLE_MODELS") == "1",
            os.getenv("CASE_REVIEW_MODEL_LOCAL_ONLY", "1") != "0",
        )

    @property
    def available(self) -> bool:
        return self._enabled

    def embed(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]:
        if not self._enabled:
            raise RuntimeError("model-backed meaning search is not enabled")
        if self._model is None:
            try:
                from sentence_transformers import SentenceTransformer
            except ImportError as exc:
                raise RuntimeError("model-backed meaning search is unavailable") from exc
            try:
                self._model = SentenceTransformer(
                    self._model_name,
                    local_files_only=self._local_only,
                    trust_remote_code=False,
                    **_in_process_device(),
                )
            except Exception as exc:
                raise RuntimeError("model-backed meaning search is unavailable") from exc
        values = self._model.encode(list(texts), normalize_embeddings=True)
        return tuple(tuple(float(value) for value in row) for row in values)


class LazySentenceTransformerReranker:
    """Environment-gated CrossEncoder-compatible reranker loaded on first use."""

    def __init__(self, model_name: str, enabled: bool, local_only: bool = True) -> None:
        self._model_name = model_name
        self._enabled = enabled
        self._local_only = local_only
        self._model = None

    @classmethod
    def from_environment(cls) -> "LazySentenceTransformerReranker":
        return cls(
            os.getenv("CASE_REVIEW_RERANKER_MODEL", DEFAULT_RERANKER_MODEL),
            os.getenv("CASE_REVIEW_ENABLE_MODELS") == "1",
            os.getenv("CASE_REVIEW_MODEL_LOCAL_ONLY", "1") != "0",
        )

    @property
    def available(self) -> bool:
        return self._enabled

    def rerank(self, query: str, candidates: Sequence[Candidate]) -> tuple[Candidate, ...]:
        if not self._enabled:
            raise RuntimeError("model-backed ranking is not enabled")
        if self._model is None:
            try:
                from sentence_transformers import CrossEncoder
            except ImportError as exc:
                raise RuntimeError("model-backed ranking is unavailable") from exc
            try:
                self._model = CrossEncoder(
                    self._model_name,
                    local_files_only=self._local_only,
                    trust_remote_code=False,
                    **_in_process_device(),
                )
            except Exception as exc:
                raise RuntimeError("model-backed ranking is unavailable") from exc
        scores = self._model.predict([(query, item.text) for item in candidates])
        ranked = [replace(item, score=float(score)) for item, score in zip(candidates, scores)]
        return tuple(sorted(ranked, key=lambda item: (-item.score, item.page_number, item.chunk_id)))


class RemoteEmbeddingAdapter:
    """Call a loopback model worker without importing Torch into the web process."""

    available = True
    batch_limit = MAX_EMBEDDING_TEXTS

    def __init__(self, base_url: str, timeout: float = 120.0, *, request=None) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._request = request or _model_request

    def embed(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]:
        # A single vector remains attached to the original evidence unit. Only
        # oversized model inputs are split; persisted ordinals/digests stay stable.
        totals: list[list[float] | None] = [None] * len(texts)
        originals: list[tuple[float, ...] | None] = [None] * len(texts)
        counts = [0] * len(texts)
        dimensions = None
        for batch, body in request_batches(text_fragments(texts), maximum=min(self.batch_limit, MAX_EMBEDDING_TEXTS)):
            payload = self._request(self.base_url, "embed", body, self.timeout)
            rows = _model_numbers(payload, "vectors", len(batch), vectors=True)
            for (ordinal, fragment), vector in zip(batch, rows):
                if dimensions is None:
                    dimensions = len(vector)
                if len(vector) != dimensions:
                    raise RuntimeError("model worker returned an invalid embedding batch")
                originals[ordinal] = vector
                counts[ordinal] += 1
                if totals[ordinal] is None:
                    totals[ordinal] = [0.0] * dimensions
                weight = len(fragment) / len(texts[ordinal])
                for index, value in enumerate(vector):
                    totals[ordinal][index] += value * weight
        vectors = []
        for total, original, count in zip(totals, originals, counts):
            if original is None or total is None:
                raise RuntimeError("model worker returned an invalid embedding batch")
            # Keep existing single-fragment behavior exactly. Multi-fragment
            # vectors use a character-weighted mean, normalized for cosine search.
            norm = math.hypot(*total) or 1.0
            vectors.append(original if count == 1 else tuple(value / norm for value in total))
        return tuple(vectors)


def _model_request(base_url: str, operation: str, body: bytes, timeout: float):
    from .retrieval_endpoints import configured_endpoint, endpoint_batch, require_index_identity
    if operation == "embed":
        require_index_identity()
    config = configured_endpoint("EMBEDDING" if operation == "embed" else "RERANK")
    if config is not None:
        return endpoint_batch(config, operation, json.loads(body))
    return _local_model_request(base_url, operation, body, timeout)


def _local_model_request(base_url, operation, body, timeout):
    request = urllib.request.Request(f"{base_url}/{operation}", data=body,
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with open_service_request(request, timeout=timeout, environment_name="CASE_INTELLIGENCE_RETRIEVAL_ALLOWED_HOSTS") as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        if exc.code in {413, 422}:
            raise RetrievalInputProblem() from None
        raise


def _model_numbers(payload, key: str, count: int, *, vectors: bool = False):
    label = "embedding" if vectors else "reranking"
    try:
        rows = payload[key]
        if not isinstance(rows, list) or len(rows) != count:
            raise ValueError
        if vectors:
            if any(not isinstance(row, list) or not row for row in rows):
                raise ValueError
            result = tuple(tuple(float(value) for value in row) for row in rows)
            finite = all(math.isfinite(value) for row in result for value in row)
        else:
            result = tuple(float(value) for value in rows)
            finite = all(math.isfinite(value) for value in result)
        if not finite:
            raise ValueError
        return result
    except (KeyError, TypeError, ValueError, OverflowError):
        raise RuntimeError(f"model worker returned an invalid {label} batch") from None


class RemoteRerankerAdapter:
    """Rerank a bounded candidate set through the loopback model worker."""

    available = True

    def __init__(self, base_url: str, timeout: float = 120.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def rerank(self, query: str, candidates: Sequence[Candidate]) -> tuple[Candidate, ...]:
        scores = [float("-inf")] * len(candidates)
        for batch, body in request_batches(text_fragments(item.text for item in candidates),
                                           maximum=MAX_RERANK_TEXTS, query=query):
            payload = _model_request(self.base_url, "rerank", body, self.timeout)
            values = _model_numbers(payload, "scores", len(batch))
            for (ordinal, _fragment), score in zip(batch, values):
                scores[ordinal] = max(scores[ordinal], score)
        ranked = [
            replace(candidate, score=float(score))
            for candidate, score in zip(candidates, scores)
        ]
        return tuple(
            sorted(ranked, key=lambda item: (-item.score, item.page_number, item.chunk_id))
        )


class InMemoryHybridBackend:
    """Matter-filtered deterministic backend used for fallback and contract tests."""

    def __init__(
        self,
        candidates: Iterable[Candidate],
        adversarial_mixed: bool = False,
        document_ids: frozenset[str] | None = None,
    ) -> None:
        self.candidates = tuple(candidates)
        self.adversarial_mixed = adversarial_mixed
        self.document_ids = document_ids
        self.calls: list[tuple[str, str]] = []

    def _rows(self, matter_id: str) -> tuple[Candidate, ...]:
        rows = (
            self.candidates
            if self.adversarial_mixed
            else tuple(item for item in self.candidates if item.matter_id == matter_id)
        )
        if self.document_ids is None:
            return tuple(rows)
        return tuple(item for item in rows if item.document_id in self.document_ids)

    def lexical(self, matter_id: str, query: str, limit: int) -> tuple[Candidate, ...]:
        self.calls.append(("lexical", matter_id))
        terms = set(_tokens(query))
        rows = []
        for item in self._rows(matter_id):
            overlap = len(terms.intersection(_tokens(item.text)))
            if overlap:
                rows.append(replace(item, score=float(overlap)))
        return tuple(sorted(rows, key=lambda item: (-item.score, item.page_number)))[:limit]

    def dense(
        self, matter_id: str, query_vector: Sequence[float], query: str, limit: int
    ) -> tuple[Candidate, ...]:
        self.calls.append(("dense", matter_id))
        terms = set(_tokens(query))
        rows = []
        embedder = DeterministicEmbeddingAdapter()
        for item in self._rows(matter_id):
            # Require one shared token in the deterministic fallback so nonsense abstains.
            if not terms.intersection(_tokens(item.text)):
                continue
            vector = embedder.embed([item.text])[0]
            similarity = sum(left * right for left, right in zip(query_vector, vector))
            rows.append(replace(item, score=similarity))
        return tuple(sorted(rows, key=lambda item: (-item.score, item.page_number)))[:limit]


class PostgresHybridBackend:
    """PostgreSQL FTS + pgvector backend with mandatory matter predicates."""

    def __init__(
        self,
        connection,
        *,
        lock=None,
        document_ids: frozenset[str] | None = None,
    ) -> None:
        self.connection = connection
        self.lock = lock
        self.document_ids = document_ids

    def _connection_guard(self):
        return self.lock if self.lock is not None else nullcontext()

    @staticmethod
    def _candidate(row) -> Candidate:
        return Candidate(
            matter_id=row[0], document_id=row[1], chunk_id=row[2], source_name=row[3],
            page_number=row[4], text=row[5], score=float(row[6]),
            line_start=row[7], line_end=row[8], source_version_id=row[9], excerpt_digest=row[10],
        )

    def lexical(self, matter_id: str, query: str, limit: int) -> tuple[Candidate, ...]:
        if self.document_ids is not None and not self.document_ids:
            return ()
        scope_sql = " AND c.document_id = ANY(%s)" if self.document_ids is not None else ""
        sql = """
            SELECT c.matter_id,c.document_id,c.chunk_id,d.display_name,p.page_number,c.text,
                   ts_rank_cd(c.search_vector, websearch_to_tsquery('english', %s)) AS score,
                   c.line_start,c.line_end,c.source_version_id,c.excerpt_digest
              FROM review_chunk c
              JOIN review_document d ON (d.matter_id,d.document_id)=(c.matter_id,c.document_id)
              JOIN review_page p ON (p.matter_id,p.document_id,p.page_id)=(c.matter_id,c.document_id,c.page_id)
             WHERE c.matter_id=%s AND c.search_vector @@ websearch_to_tsquery('english', %s)
        """ + scope_sql + """
             ORDER BY score DESC,c.chunk_id LIMIT %s
        """
        parameters: list[object] = [query, matter_id, query]
        if self.document_ids is not None:
            parameters.append(sorted(self.document_ids))
        parameters.append(limit)
        with self._connection_guard():
            with self.connection.cursor() as cursor:
                cursor.execute(sql, tuple(parameters))
                return tuple(self._candidate(row) for row in cursor.fetchall())

    def dense(
        self, matter_id: str, query_vector: Sequence[float], query: str, limit: int
    ) -> tuple[Candidate, ...]:
        if self.document_ids is not None and not self.document_ids:
            return ()
        vector = "[" + ",".join(f"{value:.8f}" for value in query_vector) + "]"
        scope_sql = " AND c.document_id = ANY(%s)" if self.document_ids is not None else ""
        sql = """
            SELECT c.matter_id,c.document_id,c.chunk_id,d.display_name,p.page_number,c.text,
                   1-(c.embedding <=> %s::vector) AS score,c.line_start,c.line_end,
                   c.source_version_id,c.excerpt_digest
              FROM review_chunk c
              JOIN review_document d ON (d.matter_id,d.document_id)=(c.matter_id,c.document_id)
              JOIN review_page p ON (p.matter_id,p.document_id,p.page_id)=(c.matter_id,c.document_id,c.page_id)
             WHERE c.matter_id=%s AND c.embedding IS NOT NULL
        """ + scope_sql + """
             ORDER BY c.embedding <=> %s::vector,c.chunk_id LIMIT %s
        """
        parameters: list[object] = [vector, matter_id]
        if self.document_ids is not None:
            parameters.append(sorted(self.document_ids))
        parameters.extend((vector, limit))
        with self._connection_guard():
            with self.connection.cursor() as cursor:
                cursor.execute(sql, tuple(parameters))
                return tuple(self._candidate(row) for row in cursor.fetchall())


class HybridRetriever:
    """Bounded lexical+dense RRF followed by bounded reranking."""

    def __init__(
        self, backend: HybridBackend, embedder: EmbeddingAdapter, reranker: RerankerAdapter,
        lane_limit: int = 100, rerank_limit: int = 40, rrf_k: int = 60,
        minimum_dense_score: float | None = None,
        minimum_rerank_score: float | None = None,
    ) -> None:
        self.backend = backend
        self.embedder = embedder
        self.reranker = reranker
        self.lane_limit = min(max(lane_limit, 1), 100)
        self.rerank_limit = min(max(rerank_limit, 1), 40)
        self.rrf_k = max(rrf_k, 1)
        if (minimum_dense_score is None) != (minimum_rerank_score is None):
            raise ValueError("dense-only evidence thresholds must be configured together")
        self.minimum_dense_score = minimum_dense_score
        self.minimum_rerank_score = minimum_rerank_score

    def search(
        self,
        matter_id: str,
        query: str,
        limit: int = 8,
        *,
        stage_callback: Callable[[str], None] | None = None,
    ) -> tuple[Candidate, ...]:
        limit = validate_primary_limit(limit)
        if not _tokens(query):
            return ()
        if stage_callback is not None:
            stage_callback("retrieving")
        query_vector = self.embedder.embed([query])[0]
        lanes = (
            tuple(self.backend.lexical(matter_id, query, self.lane_limit)),
            tuple(self.backend.dense(matter_id, query_vector, query, self.lane_limit)),
        )
        if any(item.matter_id != matter_id for lane in lanes for item in lane):
            raise RuntimeError("matter boundary violation in retrieval candidates")
        lexical_keys = {
            (item.matter_id, item.document_id, item.chunk_id) for item in lanes[0]
        }
        dense_scores = {
            (item.matter_id, item.document_id, item.chunk_id): item.score
            for item in lanes[1]
        }
        fused: dict[tuple[str, str, str], Candidate] = {}
        scores: dict[tuple[str, str, str], float] = {}
        for lane in lanes:
            for rank, item in enumerate(lane, start=1):
                key = (item.matter_id, item.document_id, item.chunk_id)
                existing = fused.get(key)
                if existing is not None and (
                    existing.matter_id,
                    existing.document_id,
                    existing.chunk_id,
                    existing.source_name,
                    existing.page_number,
                    existing.text,
                ) != (
                    item.matter_id,
                    item.document_id,
                    item.chunk_id,
                    item.source_name,
                    item.page_number,
                    item.text,
                ):
                    raise RuntimeError("conflicting retrieval candidate identity")
                fused[key] = replace(item, score=0.0)
                scores[key] = scores.get(key, 0.0) + 1.0 / (self.rrf_k + rank)
        ordered = sorted(fused, key=lambda key: (-scores[key], key))[: self.rerank_limit]
        candidates = tuple(replace(fused[key], score=scores[key]) for key in ordered)
        if stage_callback is not None:
            stage_callback("reranking")
        reranked = self.reranker.rerank(query, candidates)
        if any(item.matter_id != matter_id for item in reranked):
            raise RuntimeError("matter boundary violation after reranking")
        if self.minimum_dense_score is not None:
            minimum_rerank_score = self.minimum_rerank_score
            if minimum_rerank_score is None:  # Constructor enforces the pair.
                raise RuntimeError("dense-only evidence thresholds are incomplete")
            reranked = tuple(
                item
                for item in reranked
                if (item.matter_id, item.document_id, item.chunk_id) in lexical_keys
                or (
                    dense_scores.get(
                        (item.matter_id, item.document_id, item.chunk_id), float("-inf")
                    )
                    >= self.minimum_dense_score
                    and item.score >= minimum_rerank_score
                )
            )
        reranked = rank_exact_record_candidates(
            query,
            reranked,
            matter_id=matter_id,
        )
        reranked = rank_temporal_candidates(query, reranked, matter_id=matter_id)
        return tuple(reranked[:limit])


class PostgresMigrator:
    """Tiny idempotent migration seam; production connections persist the version table."""

    version = 1

    def __init__(
        self, migration_path: Path, *, applied_versions: set[int] | None = None,
        executor: Callable[[str], object] | None = None,
    ) -> None:
        self.migration_path = Path(migration_path)
        self.applied_versions = applied_versions if applied_versions is not None else set()
        self.executor = executor

    def migrate(self) -> bool:
        if self.version in self.applied_versions:
            return False
        sql = self.migration_path.read_text(encoding="utf-8")
        if self.executor is None:
            raise RuntimeError("migration executor is required")
        self.executor(sql)
        self.applied_versions.add(self.version)
        return True


def _packaged_postgres_migrations():
    directory = resources.files("case_intelligence").joinpath("migrations/postgresql")
    return tuple(sorted(
        (
            item for item in directory.iterdir()
            if item.name.endswith(".sql") and item.name[:4].isdigit()
        ),
        key=lambda item: item.name,
    ))


def postgres_migration_sql() -> str:
    """Load all ordered packaged migrations for parity inspection."""
    migrations = _packaged_postgres_migrations()
    return "\n".join(item.read_text(encoding="utf-8") for item in migrations)


def apply_postgres_migrations(cursor) -> tuple[int, ...]:
    """Apply only missing ordered migrations so later data is never re-narrowed."""
    cursor.execute("SELECT to_regclass('public.review_schema_migration')")
    if cursor.fetchone()[0] is None:
        applied: set[int] = set()
    else:
        cursor.execute("SELECT version FROM review_schema_migration")
        applied = {int(row[0]) for row in cursor.fetchall()}
    for migration in _packaged_postgres_migrations():
        version = int(migration.name[:4])
        if version in applied:
            continue
        cursor.execute(migration.read_text(encoding="utf-8"))
        applied.add(version)
    return tuple(sorted(applied))


def initialize_postgres_demo(
    connection,
    pages_by_matter: Mapping[str, Sequence[PdfPage]],
    *,
    embedder: EmbeddingAdapter | None = None,
) -> None:
    """Apply the owned schema and idempotently seed only the generated demo pages."""
    transaction = connection.transaction() if connection.autocommit else nullcontext()
    with transaction, connection.cursor() as cursor:
        apply_postgres_migrations(cursor)
        active_embedder = embedder or DeterministicEmbeddingAdapter()
        for matter_id, pages in pages_by_matter.items():
            vectors = active_embedder.embed([page.text for page in pages])
            if len(vectors) != len(pages) or any(len(vector) != 768 for vector in vectors):
                raise RuntimeError("embedding adapter returned an invalid page-vector batch")
            display = "North Entrance Review" if matter_id == "matter-alpha" else "South Entrance Review"
            cursor.execute(
                "INSERT INTO review_matter(matter_id,display_name) VALUES (%s,%s) "
                "ON CONFLICT (matter_id) DO UPDATE SET display_name=EXCLUDED.display_name",
                (matter_id, display),
            )
            cursor.execute(
                "INSERT INTO review_document(matter_id,document_id,display_name,media_type,page_count) "
                "VALUES (%s,'synthetic-report','Synthetic incident report.pdf','application/pdf',%s) "
                "ON CONFLICT (matter_id,document_id) DO UPDATE SET page_count=EXCLUDED.page_count",
                (matter_id, len(pages)),
            )
            cursor.execute(
                "INSERT INTO review_source_version(matter_id,document_id,source_version_id,content_digest,byte_size,media_type,storage_key,status) "
                "VALUES (%s,'synthetic-report','synthetic-v1',%s,0,'application/pdf','packaged-synthetic','ready') "
                "ON CONFLICT (matter_id,document_id,source_version_id) DO UPDATE SET status='ready'",
                (matter_id, hashlib.sha256(b''.join(page.text.encode('utf-8') for page in pages)).hexdigest()),
            )
            for page, page_vector in zip(pages, vectors):
                page_id = f"page-{page.page_number}"
                chunk_id = f"page-{page.page_number}-chunk-1"
                cursor.execute(
                    "INSERT INTO review_page(matter_id,document_id,page_id,page_number,text,source_version_id) "
                    "VALUES (%s,'synthetic-report',%s,%s,%s,'synthetic-v1') "
                    "ON CONFLICT (matter_id,document_id,page_id) DO UPDATE SET text=EXCLUDED.text",
                    (matter_id, page_id, page.page_number, page.text),
                )
                vector = "[" + ",".join(f"{value:.8f}" for value in page_vector) + "]"
                cursor.execute(
                    "INSERT INTO review_chunk(matter_id,document_id,page_id,chunk_id,ordinal,text,embedding,source_version_id,excerpt_digest) "
                    "VALUES (%s,'synthetic-report',%s,%s,0,%s,%s::vector,'synthetic-v1',%s) "
                    "ON CONFLICT (matter_id,document_id,chunk_id) DO UPDATE SET "
                    "text=EXCLUDED.text,embedding=EXCLUDED.embedding",
                    (matter_id, page_id, chunk_id, page.text, vector, hashlib.sha256(page.text.encode('utf-8')).hexdigest()),
                )
            cursor.execute(
                "INSERT INTO review_processing_state(matter_id,document_id,stage,state,completed_units,total_units,source_version_id) "
                "VALUES (%s,'synthetic-report','native extraction','ready',%s,%s,'synthetic-v1') "
                "ON CONFLICT (matter_id,document_id,stage) DO UPDATE SET "
                "state='ready',completed_units=EXCLUDED.completed_units,total_units=EXCLUDED.total_units",
                (matter_id, len(pages), len(pages)),
            )
    if not connection.autocommit:
        connection.commit()


def upsert_postgres_document(
    connection, *, matter_id: str, matter_name: str, document_id: str,
    display_name: str, media_type: str, units: Sequence[object],
    embedder: EmbeddingAdapter | None = None, source_version_id: str = "synthetic-v1",
    content_digest: str = "synthetic", byte_size: int = 0, storage_key: str = "pilot",
    ensure_schema: bool = True,
) -> None:
    """Matter-scoped generic document/page/chunk replacement for pilot uploads."""
    texts = [str(unit.text) for unit in units]
    active_embedder = embedder or DeterministicEmbeddingAdapter()
    vectors = active_embedder.embed(texts)
    if len(vectors) != len(units) or any(len(vector) != 768 for vector in vectors):
        raise RuntimeError("embedding adapter returned an invalid vector batch")
    transaction = connection.transaction() if connection.autocommit else nullcontext()
    with transaction, connection.cursor() as cursor:
        if ensure_schema:
            apply_postgres_migrations(cursor)
        cursor.execute(
            "INSERT INTO review_matter(matter_id,display_name) VALUES (%s,%s) "
            "ON CONFLICT (matter_id) DO UPDATE SET display_name=EXCLUDED.display_name",
            (matter_id, matter_name),
        )
        cursor.execute(
            "INSERT INTO review_document(matter_id,document_id,display_name,media_type,page_count,current_source_version_id) "
            "VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (matter_id,document_id) DO UPDATE SET "
            "display_name=EXCLUDED.display_name,media_type=EXCLUDED.media_type,page_count=EXCLUDED.page_count,"
            "current_source_version_id=EXCLUDED.current_source_version_id",
            (matter_id, document_id, display_name, media_type, len(units), source_version_id),
        )
        cursor.execute(
            "INSERT INTO review_source_version(matter_id,document_id,source_version_id,content_digest,byte_size,media_type,storage_key,status) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,'ready') ON CONFLICT (matter_id,document_id,source_version_id) "
            "DO UPDATE SET status='ready'",
            (matter_id, document_id, source_version_id, content_digest, byte_size, media_type, storage_key),
        )
        cursor.execute(
            "DELETE FROM review_page WHERE matter_id=%s AND document_id=%s",
            (matter_id, document_id),
        )
        for ordinal, (unit, vector_values) in enumerate(zip(units, vectors)):
            page_id = f"unit-{ordinal + 1}"
            chunk_id = f"chunk-{ordinal + 1}"
            cursor.execute(
                "INSERT INTO review_page(matter_id,document_id,page_id,page_number,text,source_version_id,line_start,line_end) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                (matter_id, document_id, page_id, int(unit.number), str(unit.text), source_version_id,
                 getattr(unit, "line_start", None), getattr(unit, "line_end", None)),
            )
            vector = "[" + ",".join(f"{value:.8f}" for value in vector_values) + "]"
            cursor.execute(
                "INSERT INTO review_chunk(matter_id,document_id,page_id,chunk_id,ordinal,text,embedding,line_start,line_end,source_version_id,excerpt_digest) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s::vector,%s,%s,%s,%s)",
                (matter_id, document_id, page_id, chunk_id, ordinal, str(unit.text), vector,
                 getattr(unit, "line_start", None), getattr(unit, "line_end", None), source_version_id,
                 getattr(unit, "excerpt_digest", hashlib.sha256(str(unit.text).encode("utf-8")).hexdigest())),
            )
        cursor.execute(
            "INSERT INTO review_processing_state(matter_id,document_id,stage,state,completed_units,total_units,source_version_id) "
            "VALUES (%s,%s,'native extraction','ready',%s,%s,%s) ON CONFLICT (matter_id,document_id,stage) "
            "DO UPDATE SET state='ready',completed_units=EXCLUDED.completed_units,total_units=EXCLUDED.total_units,"
            "source_version_id=EXCLUDED.source_version_id",
            (matter_id, document_id, len(units), len(units), source_version_id),
        )
    if not connection.autocommit:
        connection.commit()

def delete_postgres_document(connection, *, matter_id: str, document_id: str) -> None:
    """Delete one document and all derived rows under an explicit matter fence."""
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "DELETE FROM review_document WHERE matter_id=%s AND document_id=%s",
                (matter_id, document_id),
            )
        connection.commit()
    except Exception:
        connection.rollback()
        raise


def delete_postgres_matter_documents(connection, *, matter_id: str) -> None:
    """Clear one development matter before rebuilding its derived upload index."""
    try:
        with connection.cursor() as cursor:
            cursor.execute("DELETE FROM review_document WHERE matter_id=%s", (matter_id,))
        connection.commit()
    except Exception:
        connection.rollback()
        raise


def delete_postgres_matter(connection, *, matter_id: str) -> None:
    """Delete one exact workbench matter and every derived PostgreSQL row."""

    if not isinstance(matter_id, str) or not _WORKBENCH_MATTER_ID.fullmatch(matter_id):
        raise ValueError("workbench matter identity is invalid")
    try:
        with connection.cursor() as cursor:
            cursor.execute("DELETE FROM review_matter WHERE matter_id=%s", (matter_id,))
        connection.commit()
    except Exception:
        connection.rollback()
        raise


def retain_postgres_workbench_matters(
    connection,
    *,
    matter_ids: Sequence[str],
) -> None:
    """Remove derived rows for matters absent from the authoritative workspace."""
    retained = tuple(dict.fromkeys(matter_ids))
    if any(
        not isinstance(matter_id, str)
        or not _WORKBENCH_MATTER_ID.fullmatch(matter_id)
        for matter_id in retained
    ):
        raise ValueError("retained workbench matter identities are invalid")
    transaction = connection.transaction() if connection.autocommit else nullcontext()
    with transaction, connection.cursor() as cursor:
        if retained:
            cursor.execute(
                "DELETE FROM review_matter "
                "WHERE matter_id ~ '^ci-matter-[0-9a-f]{32}$' "
                "AND NOT (matter_id = ANY(%s))",
                (list(retained),),
            )
        else:
            cursor.execute(
                "DELETE FROM review_matter "
                "WHERE matter_id ~ '^ci-matter-[0-9a-f]{32}$'"
            )
    if not connection.autocommit:
        connection.commit()


def connect_postgres_from_environment():
    """Return an optional development connection; failure intentionally triggers fallback."""
    dsn = os.getenv("CASE_REVIEW_POSTGRES_DSN")
    if not dsn:
        return None
    try:
        import psycopg
        return psycopg.connect(dsn, connect_timeout=1)
    except Exception:
        return None
