"""Grade defense-review questions against a versioned synthetic discovery set.

The stub mode answers from retrieved reference spans and makes no model call.
Configured mode uses the generator selected by CASE_INTELLIGENCE_GENERATOR_*.
Deterministic scores stay separate from the optional raw LLM judge.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from dataclasses import dataclass
from pathlib import Path
import re
import sys
import time
from typing import Any, Callable, Mapping, Sequence

from . import generation
from .service_endpoints import open_service_request

from .generation import (
    EvidenceItem,
    GenerationRejected,
    GenerationUnavailable,
    GroundedGenerationService,
    _TRANSCRIPT_ATTRIBUTION,
    generator_from_environment,
)


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SUITE = (ROOT / "benchmarks" / "defense-workflow" / "v2" / "suite").with_suffix(".json")
_POSTGRES_DSN_ENV = "CASE_REVIEW_POSTGRES_TEST_DSN"
_LEARNED_DENSE_FLOOR = 0.72
_LEARNED_RERANK_FLOOR = 0.80
QUESTION_TYPES = (
    "fact_lookup",
    "chronology",
    "contradiction",
    "impeachment",
    "media_transcript",
    "not_in_record",
)
DEFAULT_TOP_K = 8
MAX_TOP_K = 12
_TOKEN = re.compile(r"[a-z0-9]+(?:[:\-][a-z0-9]+)*")
_STOP = frozenset(
    "a an the of to and or in on at for from with by that this was were is are "
    "be as it its not no did do does what when who which how about between into "
    "than then their them they she he his her had have has would could should "
    "after before during whose using whether".split()
)
_SUITE_KEYS = {
    "schema_version", "suite_id", "suite_version", "synthetic_only", "description",
    "fingerprint", "passages", "questions",
}
_PASSAGE_KEYS = {"passage_id", "matter_id", "path", "sha256", "evidence_kind", "locator"}
_QUESTION_KEYS = {
    "question_id", "matter_id", "type", "question", "expect_abstention",
    "required_facts", "forbidden_facts",
}
_QUESTION_OPTIONAL = frozenset({"near_misses"})
_COMPARE_METRICS = (
    ("answer_correct", "Answer"),
    ("citation_support", "Citation"),
    ("abstention_correct", "Abstention"),
    ("retrieval_hit", "Retrieval"),
)
_FACT_KEYS = {"fact_id", "answer_text", "supports"}
_SUPPORT_KEYS = {"passage_id", "support_text"}


class DefenseEvalError(ValueError):
    """The suite, arguments, or configured generator cannot be evaluated."""


@dataclass(frozen=True)
class Support:
    passage_id: str
    support_text: str


@dataclass(frozen=True)
class Fact:
    fact_id: str
    answer_text: str
    supports: tuple[Support, ...]


@dataclass(frozen=True)
class Question:
    question_id: str
    matter_id: str
    type: str
    text: str
    expect_abstention: bool
    required_facts: tuple[Fact, ...]
    forbidden_facts: tuple[str, ...]
    near_misses: tuple[str, ...] = ()


@dataclass(frozen=True)
class Passage:
    passage_id: str
    matter_id: str
    path: str
    sha256: str
    evidence_kind: str
    locator: str
    text: str


@dataclass(frozen=True)
class Suite:
    suite_id: str
    suite_version: str
    fingerprint: str
    description: str
    passages: tuple[Passage, ...]
    questions: tuple[Question, ...]

    def passage(self, passage_id: str) -> Passage:
        return next(item for item in self.passages if item.passage_id == passage_id)


@dataclass(frozen=True)
class Citation:
    passage_id: str
    support_text: str


@dataclass(frozen=True)
class ModelAnswer:
    abstained: bool
    text: str
    citations: tuple[Citation, ...]
    error: str = ""
    rejection: str = ""


Answerer = Callable[[Question, Sequence[Passage]], ModelAnswer]
Judge = Callable[[Question, ModelAnswer], Mapping[str, Any]]
Retriever = Callable[[Sequence[Passage], str, int], Sequence[Passage]]


def normalize(value: str) -> str:
    return " ".join(value.casefold().split())


def tokenize(value: str) -> tuple[str, ...]:
    """Content tokens, keeping identifiers such as 09:14 and DEMO-HS-271 intact."""
    tokens: list[str] = []
    for token in _TOKEN.findall(value.casefold()):
        tokens.append(token)
        if "-" in token and not any(character.isdigit() for character in token):
            tokens.extend(part for part in token.split("-") if part)
    return tuple(token for token in tokens if token not in _STOP and len(token) > 1)


def suite_fingerprint(suite: Mapping[str, Any]) -> str:
    body = {key: value for key, value in suite.items() if key != "fingerprint"}
    encoded = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _require_keys(
    value: Mapping[str, Any],
    expected: set[str],
    label: str,
    optional: frozenset[str] = frozenset(),
) -> None:
    keys = set(value)
    if not expected <= keys or not keys <= expected | set(optional):
        raise DefenseEvalError(f"{label} keys do not match the versioned suite")


def _relative_path(value: str) -> Path:
    path = Path(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise DefenseEvalError("passage paths must stay inside the repository")
    return path


def load_suite(path: Path | None = None, *, root: Path = ROOT) -> Suite:
    suite_path = Path(path) if path is not None else DEFAULT_SUITE
    try:
        payload = json.loads(suite_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise DefenseEvalError("the defense-workflow suite is not readable JSON") from exc
    if not isinstance(payload, dict):
        raise DefenseEvalError("the defense-workflow suite must be a JSON object")
    _require_keys(payload, _SUITE_KEYS, "suite")
    if (
        payload["schema_version"] != "1"
        or payload["suite_id"] != "defense-workflow"
        or payload["synthetic_only"] is not True
        or not isinstance(payload["suite_version"], str)
        or not payload["description"]
    ):
        raise DefenseEvalError("unsupported defense-workflow suite")
    if suite_fingerprint(payload) != payload["fingerprint"]:
        raise DefenseEvalError("defense-workflow suite fingerprint mismatch")
    passages = _load_passages(payload["passages"], root)
    questions = _load_questions(
        payload["questions"], passages, require_near_misses=payload["suite_version"] != "1",
    )
    types = {question.type for question in questions}
    if types != set(QUESTION_TYPES):
        raise DefenseEvalError("the suite must include every defense question type")
    return Suite(
        payload["suite_id"], payload["suite_version"], payload["fingerprint"],
        payload["description"], passages, questions,
    )


def _load_passages(records: object, root: Path) -> tuple[Passage, ...]:
    if not isinstance(records, list) or not records:
        raise DefenseEvalError("the suite needs passages")
    passages: list[Passage] = []
    seen: set[str] = set()
    for record in records:
        if not isinstance(record, dict):
            raise DefenseEvalError("a passage record is malformed")
        _require_keys(record, _PASSAGE_KEYS, "passage")
        passage_id = record["passage_id"]
        if not isinstance(passage_id, str) or passage_id in seen:
            raise DefenseEvalError("passage identifiers must be unique")
        seen.add(passage_id)
        if record["evidence_kind"] not in {"document", "transcript"}:
            raise DefenseEvalError("unsupported evidence kind")
        relative = _relative_path(record["path"])
        try:
            data = (root / relative).read_bytes()
        except OSError as exc:
            raise DefenseEvalError("a suite passage is missing") from exc
        if b"\x00" in data:
            raise DefenseEvalError("a suite passage contains a NUL byte")
        digest = hashlib.sha256(data).hexdigest()
        if digest != record["sha256"]:
            raise DefenseEvalError(f"passage {passage_id} bytes do not match the suite")
        try:
            text = data.decode("utf-8")
        except UnicodeError as exc:
            raise DefenseEvalError(f"passage {passage_id} is not UTF-8 text") from exc
        passages.append(Passage(
            passage_id, record["matter_id"], record["path"], digest,
            record["evidence_kind"], record["locator"], text,
        ))
    return tuple(passages)


def _load_questions(
    records: object,
    passages: Sequence[Passage],
    *,
    require_near_misses: bool,
) -> tuple[Question, ...]:
    if not isinstance(records, list) or not records:
        raise DefenseEvalError("the suite needs questions")
    by_id = {passage.passage_id: passage for passage in passages}
    questions: list[Question] = []
    seen: set[str] = set()
    for record in records:
        if not isinstance(record, dict):
            raise DefenseEvalError("a question record is malformed")
        _require_keys(record, _QUESTION_KEYS, "question", optional=_QUESTION_OPTIONAL)
        question_id = record["question_id"]
        if not isinstance(question_id, str) or question_id in seen:
            raise DefenseEvalError("question identifiers must be unique")
        seen.add(question_id)
        if record["type"] not in QUESTION_TYPES:
            raise DefenseEvalError(f"unsupported question type for {question_id}")
        if type(record["expect_abstention"]) is not bool:
            raise DefenseEvalError("expect_abstention must be a boolean")
        if record["matter_id"] not in {passage.matter_id for passage in passages}:
            raise DefenseEvalError(f"{question_id} names an unknown matter")
        facts = _load_facts(record["required_facts"], by_id, question_id, record["matter_id"])
        forbidden = record["forbidden_facts"]
        if not isinstance(forbidden, list) or any(not isinstance(item, str) or not item for item in forbidden):
            raise DefenseEvalError(f"{question_id} has a malformed forbidden fact")
        expects_absence = record["type"] == "not_in_record"
        if record["expect_abstention"] != expects_absence or bool(facts) == expects_absence:
            raise DefenseEvalError(f"{question_id} mixes abstention with required facts")
        near_misses = _load_near_misses(record, by_id, facts, require_near_misses)
        questions.append(Question(
            question_id, record["matter_id"], record["type"], record["question"],
            record["expect_abstention"], facts, tuple(forbidden), near_misses,
        ))
    return tuple(questions)


def _load_near_misses(
    record: Mapping[str, Any],
    passages: Mapping[str, Passage],
    facts: Sequence[Fact],
    required: bool,
) -> tuple[str, ...]:
    question_id = record["question_id"]
    if "near_misses" not in record:
        if required:
            raise DefenseEvalError(f"{question_id} needs a near-miss passage")
        return ()
    near = record["near_misses"]
    if not isinstance(near, list) or any(not isinstance(item, str) or not item for item in near):
        raise DefenseEvalError(f"{question_id} has a malformed near miss")
    if len(near) != len(set(near)) or (required and not near):
        raise DefenseEvalError(f"{question_id} needs distinct near-miss passages")
    support_ids = {support.passage_id for fact in facts for support in fact.supports}
    for passage_id in near:
        passage = passages.get(passage_id)
        if passage is None or passage.matter_id != record["matter_id"] or passage_id in support_ids:
            raise DefenseEvalError(f"{question_id} near miss is not a competing passage")
        if any(
            normalize(support.support_text) in normalize(passage.text)
            for fact in facts for support in fact.supports
        ):
            raise DefenseEvalError(f"{question_id} near miss contains a reference span")
    return tuple(near)


def _load_facts(records: object, passages: Mapping[str, Passage], question_id: str,
                matter_id: str) -> tuple[Fact, ...]:
    if not isinstance(records, list):
        raise DefenseEvalError(f"{question_id} facts are malformed")
    facts: list[Fact] = []
    seen: set[str] = set()
    for record in records:
        if not isinstance(record, dict):
            raise DefenseEvalError(f"{question_id} has a malformed fact")
        _require_keys(record, _FACT_KEYS, "fact")
        fact_id = record["fact_id"]
        if not isinstance(fact_id, str) or fact_id in seen or not record["answer_text"]:
            raise DefenseEvalError(f"{question_id} has a duplicate or empty fact")
        seen.add(fact_id)
        supports = record["supports"]
        if not isinstance(supports, list) or not supports:
            raise DefenseEvalError(f"{question_id} fact {fact_id} needs support")
        loaded: list[Support] = []
        for support in supports:
            if not isinstance(support, dict):
                raise DefenseEvalError(f"{question_id} has malformed support")
            _require_keys(support, _SUPPORT_KEYS, "support")
            passage = passages.get(support["passage_id"])
            if passage is not None and passage.matter_id != matter_id:
                raise DefenseEvalError(f"{question_id} support belongs to another matter")
            if passage is None or normalize(support["support_text"]) not in normalize(passage.text):
                raise DefenseEvalError(
                    f"{question_id} support for {fact_id} is not in the named passage"
                )
            loaded.append(Support(support["passage_id"], support["support_text"]))
        facts.append(Fact(fact_id, record["answer_text"], tuple(loaded)))
    return tuple(facts)


def rank_passages(passages: Sequence[Passage], query: str, top_k: int) -> tuple[Passage, ...]:
    """Deterministic lexical rank within one matter. No model and no network."""
    if not 1 <= top_k <= MAX_TOP_K:
        raise DefenseEvalError(f"top_k must be from 1 to {MAX_TOP_K}")
    prepared = [(passage, set(tokenize(passage.text)), tokenize(passage.text)) for passage in passages]
    document_frequency: dict[str, int] = {}
    for _, seen, _tokens in prepared:
        for token in seen:
            document_frequency[token] = document_frequency.get(token, 0) + 1
    total = len(prepared)

    def weight(token: str) -> float:
        return math.log((total + 1) / (document_frequency.get(token, 0) + 1)) + 1

    query_tokens = tokenize(query)
    query_terms = set(query_tokens)
    ranked: list[tuple[float, Passage]] = []
    for passage, seen, passage_tokens in prepared:
        score = sum(weight(token) for token in query_terms if token in seen)
        pairs = set(zip(passage_tokens, passage_tokens[1:]))
        score += 0.35 * sum(
            weight(left) + weight(right)
            for left, right in zip(query_tokens, query_tokens[1:])
            if (left, right) in pairs
        )
        ranked.append((score, passage))
    ranked.sort(key=lambda item: (-item[0], item[1].passage_id))
    return tuple(passage for _score, passage in ranked[:top_k])


def lexical_retriever_record() -> dict[str, Any]:
    return {
        "name": "lexical",
        "index": "in-process",
        "embedder": None,
        "reranker": None,
        "learned_thresholds": False,
    }


def defense_matter_database_id(matter_id: str) -> str:
    """Stable workbench matter id for one synthetic suite matter."""
    digest = hashlib.sha256(f"defense-workflow:{matter_id}".encode("utf-8")).hexdigest()
    return f"ci-matter-{digest[:32]}"


def _healthy_retrieval_worker() -> str | None:
    """Match production: a loopback worker is learned only when both models are loaded."""
    raw = os.getenv("CASE_INTELLIGENCE_RETRIEVAL_WORKER_URL", "").strip()
    if not raw:
        return None
    try:
        from .service_endpoints import validate_service_endpoint
        endpoint = validate_service_endpoint(
            raw,
            environment_name="CASE_INTELLIGENCE_RETRIEVAL_ALLOWED_HOSTS",
            label="retrieval worker endpoint",
            schemes=frozenset({"http"}),
            origin_only=True,
        )
    except ValueError:
        return None
    try:
        with open_service_request(
            endpoint + "/health", timeout=2,
            environment_name="CASE_INTELLIGENCE_RETRIEVAL_ALLOWED_HOSTS",
        ) as response:
            payload = json.load(response)
    except (OSError, ValueError):
        return None
    if (
        isinstance(payload, dict)
        and payload.get("status") == "ok"
        and payload.get("embedding_loaded") is True
        and payload.get("reranker_loaded") is True
    ):
        return endpoint
    return None


def hybrid_adapters(worker_url: str | None):
    """Production adapter pair. Learned floors apply only with a healthy worker."""
    from .review_bench_v2 import (
        DeterministicEmbeddingAdapter,
        DeterministicReranker,
        RemoteEmbeddingAdapter,
        RemoteRerankerAdapter,
    )
    if worker_url:
        return (
            RemoteEmbeddingAdapter(worker_url),
            RemoteRerankerAdapter(worker_url),
            {
                "minimum_dense_score": _LEARNED_DENSE_FLOOR,
                "minimum_rerank_score": _LEARNED_RERANK_FLOOR,
            },
        )
    return DeterministicEmbeddingAdapter(), DeterministicReranker(), {}


class PostgresHybridRetriever:
    """Search indexed passages with HybridRetriever on PostgreSQL.

    Each passage is one text unit in the product FTS and pgvector index.
    Search is lexical full-text plus dense cosine, fused, then reranked.
    Without a healthy retrieval worker the embedder and reranker are the
    deterministic adapters production uses when learned retrieval is off.
    The database id is a ci-matter hash, and those rows are removed on close.
    """

    def __init__(self, suite: Suite, dsn: str | None = None) -> None:
        configured = (dsn if dsn is not None else os.getenv(_POSTGRES_DSN_ENV, "")).strip()
        if not configured:
            raise DefenseEvalError("PostgreSQL retrieval needs CASE_REVIEW_POSTGRES_TEST_DSN")
        self.suite = suite
        self._dsn = configured
        self._database_ids = {
            matter_id: defense_matter_database_id(matter_id)
            for matter_id in {passage.matter_id for passage in suite.passages}
        }
        self._connection = None
        self._delete = None
        self._retriever = None
        self.record = {
            "name": "postgres-hybrid",
            "index": "postgresql-fts-pgvector",
            "embedder": None,
            "reranker": None,
            "learned_thresholds": False,
        }
        self._open()

    def _open(self) -> None:
        try:
            import psycopg
        except ImportError as exc:
            raise DefenseEvalError("PostgreSQL retrieval needs the postgres extra") from exc
        from .pilot_uploads import PilotUnit
        from .review_bench_v2 import (
            HybridRetriever,
            PostgresHybridBackend,
            apply_postgres_migrations,
            delete_postgres_matter,
            upsert_postgres_document,
        )
        try:
            connection = psycopg.connect(self._dsn, connect_timeout=2)
        except Exception as exc:
            raise DefenseEvalError(
                "PostgreSQL retrieval could not open the configured database"
            ) from exc
        self._connection = connection
        self._delete = delete_postgres_matter
        worker = _healthy_retrieval_worker()
        embedder, reranker, floors = hybrid_adapters(worker)
        self.record = {
            "name": "postgres-hybrid",
            "index": "postgresql-fts-pgvector",
            "embedder": type(embedder).__name__,
            "reranker": type(reranker).__name__,
            "learned_thresholds": bool(floors),
        }
        try:
            with connection.cursor() as cursor:
                apply_postgres_migrations(cursor)
            connection.commit()
            for database_id in self._database_ids.values():
                delete_postgres_matter(connection, matter_id=database_id)
            self._index(connection, PilotUnit, upsert_postgres_document, embedder)
            self._retriever = HybridRetriever(
                PostgresHybridBackend(connection), embedder, reranker, **floors,
            )
        except Exception as exc:
            self.close()
            if isinstance(exc, DefenseEvalError):
                raise
            raise DefenseEvalError(
                "PostgreSQL retrieval could not index the synthetic suite"
            ) from exc

    def _index(self, connection, unit_type, upsert, embedder) -> None:
        grouped: dict[str, list[Passage]] = {}
        for passage in self.suite.passages:
            grouped.setdefault(passage.matter_id, []).append(passage)
        for matter_id, passages in grouped.items():
            database_id = self._database_ids[matter_id]
            for passage in passages:
                upsert(
                    connection,
                    matter_id=database_id,
                    matter_name=matter_id,
                    document_id=passage.passage_id,
                    display_name=Path(passage.path).name,
                    media_type="text/plain",
                    units=(unit_type(1, passage.text, 1, passage.text.count("\n") + 1),),
                    embedder=embedder,
                    content_digest=passage.sha256,
                    byte_size=len(passage.text.encode("utf-8")),
                    storage_key="defense-eval-synthetic",
                )

    def close(self) -> None:
        connection = self._connection
        delete = self._delete
        self._connection = None
        self._retriever = None
        if connection is None:
            return
        failed = False
        try:
            if delete is not None:
                for database_id in self._database_ids.values():
                    delete(connection, matter_id=database_id)
        except Exception:
            failed = True
            try:
                connection.rollback()
            except Exception:
                pass
        finally:
            connection.close()
        if failed:
            raise DefenseEvalError("PostgreSQL retrieval could not remove the synthetic matter")

    def __enter__(self) -> "PostgresHybridRetriever":
        return self

    def __exit__(self, exc_type, exc, traceback) -> bool:
        self.close()
        return False

    def __call__(self, passages: Sequence[Passage], query: str, top_k: int) -> tuple[Passage, ...]:
        if self._retriever is None or not passages:
            return ()
        matter_id = passages[0].matter_id
        if any(passage.matter_id != matter_id for passage in passages):
            raise DefenseEvalError("PostgreSQL retrieval stays inside one matter")
        if not 1 <= top_k <= MAX_TOP_K:
            raise DefenseEvalError(f"top_k must be from 1 to {MAX_TOP_K}")
        by_id = {passage.passage_id: passage for passage in passages}
        try:
            hits = self._retriever.search(self._database_ids[matter_id], query, top_k)
        except Exception as exc:
            raise DefenseEvalError("PostgreSQL retrieval search failed") from exc
        selected: list[Passage] = []
        for hit in hits:
            passage = by_id.get(hit.document_id)
            if passage is None or passage.matter_id != matter_id:
                raise DefenseEvalError(
                    "PostgreSQL retrieval returned a passage outside the question matter"
                )
            selected.append(passage)
        return tuple(selected)


def _contains(haystack: str, needle: str) -> bool:
    return bool(needle) and normalize(needle) in normalize(haystack)


class ReferenceStub:
    """Quote each retrieved reference span, or abstain when the span was not retrieved.

    This is the CI answerer. It checks the harness and whichever retriever ran.
    It is not a model quality result.
    """

    def __call__(self, question: Question, passages: Sequence[Passage]) -> ModelAnswer:
        if question.expect_abstention:
            return ModelAnswer(True, "Not in the record.", ())
        citations: list[Citation] = []
        parts: list[str] = []
        by_id = {passage.passage_id: passage for passage in passages}
        for fact in question.required_facts:
            match = next((
                support for support in fact.supports
                if support.passage_id in by_id
                and _contains(by_id[support.passage_id].text, support.support_text)
            ), None)
            if match is None:
                return ModelAnswer(
                    True,
                    "The retrieved records do not include the reference passage.",
                    (),
                )
            citations.append(Citation(match.passage_id, match.support_text))
            parts.append(fact.answer_text)
        return ModelAnswer(False, " ".join(parts), tuple(citations))


class ConfiguredAnswerer:
    """Ask the product generator over the passages the harness retrieved."""

    def __init__(self, service: GroundedGenerationService) -> None:
        self.service = service

    def __call__(self, question: Question, passages: Sequence[Passage]) -> ModelAnswer:
        mapping: dict[str, str] = {}
        evidence: list[EvidenceItem] = []
        for index, passage in enumerate(passages, start=1):
            evidence_id = f"S{index}"
            mapping[evidence_id] = passage.passage_id
            evidence.append(EvidenceItem(
                evidence_id=evidence_id,
                source_name=Path(passage.path).name,
                location=passage.locator,
                excerpt=passage.text,
                evidence_kind=passage.evidence_kind,
                document_id=passage.passage_id,
            ))
        try:
            verified = self.service.answer(question.text, evidence)
        # The baseline checkout may predate GenerationInvalidContent, so resolve it at run time.
        except (GenerationRejected, getattr(generation, "GenerationInvalidContent", GenerationRejected)) as exc:
            return ModelAnswer(False, "", (), rejection=exc.__class__.__name__)
        except (GenerationUnavailable, ValueError, OSError) as exc:
            return ModelAnswer(False, "", (), error=exc.__class__.__name__)
        citations: list[Citation] = []
        seen: set[tuple[str, str]] = set()
        claims = list(verified.claims)
        limitation = verified.source_limitation if verified.verification_notice else verified.limitation
        if limitation is not None:
            claims.append(limitation)
        by_id = {passage.passage_id: passage for passage in passages}
        for claim in claims:
            for evidence_id in claim.evidence_ids:
                passage = by_id[mapping[evidence_id]]
                text = claim.text
                if passage.evidence_kind == "transcript":
                    attribution = _TRANSCRIPT_ATTRIBUTION.match(text)
                    if attribution is not None:
                        text = text[attribution.end():].lstrip(" ,:;-\u2013\u2014")
                key = (passage.passage_id, text)
                if key not in seen:
                    seen.add(key)
                    citations.append(Citation(*key))
        return ModelAnswer(not verified.answerable, verified.text, tuple(citations))


def _fact_cited(fact: Fact, citations: Sequence[Citation], passages: Mapping[str, Passage]) -> bool:
    for citation in citations:
        passage = passages.get(citation.passage_id)
        if passage is None or not _contains(passage.text, citation.support_text):
            continue
        for support in fact.supports:
            if citation.passage_id == support.passage_id and _contains(citation.support_text, support.support_text):
                return True
    return False


def _citation_allowed(citation: Citation, question: Question, passages: Mapping[str, Passage]) -> bool:
    passage = passages.get(citation.passage_id)
    if passage is None or not _contains(passage.text, citation.support_text):
        return False
    return any(
        citation.passage_id == support.passage_id and _contains(citation.support_text, support.support_text)
        for fact in question.required_facts
        for support in fact.supports
    )


def score_question(
    question: Question,
    answer: ModelAnswer,
    retrieved: Sequence[Passage],
    passages: Mapping[str, Passage],
    latency_ms: int,
) -> dict[str, Any]:
    retrieved_ids = [passage.passage_id for passage in retrieved]
    if question.required_facts:
        retrieval_hit = all(
            any(support.passage_id in retrieved_ids for support in fact.supports)
            for fact in question.required_facts
        )
    else:
        retrieval_hit = None
    forbidden = any(_contains(answer.text, fact) for fact in question.forbidden_facts)
    abstention_correct = answer.abstained is question.expect_abstention and not (answer.error or answer.rejection)
    if answer.error or answer.rejection:
        answer_correct = False
        citation_support = False
        missed = [fact.fact_id for fact in question.required_facts]
        unsupported = [citation.passage_id for citation in answer.citations]
    elif question.expect_abstention:
        answer_correct = answer.abstained and not forbidden
        citation_support = answer.abstained and not answer.citations
        missed = []
        unsupported = [citation.passage_id for citation in answer.citations]
    else:
        present = all(_contains(answer.text, fact.answer_text) for fact in question.required_facts)
        answer_correct = (not answer.abstained) and present and not forbidden
        missed = [
            fact.fact_id for fact in question.required_facts
            if not _fact_cited(fact, answer.citations, passages)
        ]
        unsupported = [
            citation.passage_id for citation in answer.citations
            if not _citation_allowed(citation, question, passages)
        ]
        citation_support = not answer.abstained and not missed and not unsupported
    return {
        "question_id": question.question_id,
        "type": question.type,
        "matter_id": question.matter_id,
        "expect_abstention": question.expect_abstention,
        "abstained": answer.abstained,
        "answer_correct": answer_correct,
        "citation_support": citation_support,
        "abstention_correct": abstention_correct,
        "retrieval_hit": retrieval_hit,
        "latency_ms": latency_ms,
        "retrieved_passage_ids": retrieved_ids,
        "cited_passage_ids": [citation.passage_id for citation in answer.citations],
        "missed_facts": missed,
        "unsupported_citations": unsupported,
        "unsupported_citation_count": len(unsupported),
        "answer_text": answer.text,
        "error": answer.error,
        "rejection": answer.rejection,
        "llm_judge": None,
    }


def run_eval(
    suite: Suite,
    answerer: Answerer,
    *,
    top_k: int = DEFAULT_TOP_K,
    judge: Judge | None = None,
    model: Mapping[str, Any] | None = None,
    retriever: Retriever = rank_passages,
    retriever_record: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    by_id = {passage.passage_id: passage for passage in suite.passages}
    rows: list[dict[str, Any]] = []
    for question in suite.questions:
        matter = tuple(passage for passage in suite.passages if passage.matter_id == question.matter_id)
        started = time.perf_counter()
        retrieved = tuple(retriever(matter, question.text, top_k))
        try:
            answer = answerer(question, retrieved)
        except Exception as exc:  # a caller-supplied answerer should not abort the card
            answer = ModelAnswer(False, "", (), error=exc.__class__.__name__)
        latency_ms = max(0, round((time.perf_counter() - started) * 1000))
        row = score_question(question, answer, retrieved, by_id, latency_ms)
        if judge is not None:
            try:
                judgment = dict(judge(question, answer))
            except Exception as exc:  # the judge is optional and must not erase deterministic scores
                judgment = {"error": exc.__class__.__name__}
            judgment["label"] = "optional_llm_judge"
            judgment["included_in_deterministic_totals"] = False
            row["llm_judge"] = judgment
        rows.append(row)
    return _scorecard(
        suite, rows, model or {"mode": "unspecified"}, judge is not None,
        retriever_record or lexical_retriever_record(),
    )


def _rate(numerator: int, denominator: int) -> float | None:
    if denominator == 0:
        return None
    return round(numerator / denominator, 4)


def _scorecard(
    suite: Suite,
    rows: Sequence[Mapping[str, Any]],
    model: Mapping[str, Any],
    judged: bool,
    retriever: Mapping[str, Any],
) -> dict[str, Any]:
    total = len(rows)
    retrieval_rows = [row for row in rows if row["retrieval_hit"] is not None]
    retrieval_hits = sum(row["retrieval_hit"] is True for row in retrieval_rows)
    latencies = [int(row["latency_ms"]) for row in rows]
    return {
        "suite_id": suite.suite_id,
        "suite_version": suite.suite_version,
        "fingerprint": suite.fingerprint,
        "scoring": "deterministic",
        "measurement_only": True,
        "release_threshold": None,
        "model": dict(model),
        "retriever": dict(retriever),
        "llm_judge": {
            "enabled": judged,
            "label": "optional_llm_judge",
            "included_in_deterministic_totals": False,
        },
        "methods": {
            "answer_correctness": "normalized required-fact containment; any forbidden fact fails",
            "citation_support": "each cited support span occurs in that passage, and every reference span is cited",
            "abstention_correctness": "the abstention flag matches the reference expectation",
            "latency": "wall-clock milliseconds for retrieval plus answering",
            "llm_judge": "optional raw generator judgment, excluded from deterministic totals",
        },
        "aggregates": {
            "questions": total,
            "errors": sum(bool(row["error"]) for row in rows),
            "rejections": sum(bool(row["rejection"]) for row in rows),
            "unsupported_citation_count": sum(row["unsupported_citation_count"] for row in rows),
            "answer_correct": sum(bool(row["answer_correct"]) for row in rows),
            "answer_correct_rate": _rate(sum(bool(row["answer_correct"]) for row in rows), total),
            "citation_support": sum(bool(row["citation_support"]) for row in rows),
            "citation_support_rate": _rate(sum(bool(row["citation_support"]) for row in rows), total),
            "abstention_correct": sum(bool(row["abstention_correct"]) for row in rows),
            "abstention_correct_rate": _rate(sum(bool(row["abstention_correct"]) for row in rows), total),
            "retrieval_hit": retrieval_hits,
            "retrieval_applicable": len(retrieval_rows),
            "retrieval_hit_rate": _rate(retrieval_hits, len(retrieval_rows)),
            "latency_ms_mean": round(sum(latencies) / total, 1) if total else 0,
            "latency_ms_max": max(latencies) if latencies else 0,
        },
        "questions": list(rows),
    }


def render_markdown(scorecard: Mapping[str, Any]) -> str:
    model = scorecard["model"]
    aggregates = scorecard["aggregates"]
    mode = model.get("mode", "unspecified")
    lines = [
        "# Defense workflow scorecard",
        "",
        f"Suite `{scorecard['suite_id']}` version `{scorecard['suite_version']}` "
        f"fingerprint `{scorecard['fingerprint']}`.",
        "",
        f"Mode: `{mode}`. Scoring: deterministic. "
        "These rates are measurements, not a release threshold.",
        "",
    ]
    retriever = scorecard.get("retriever") or {}
    if retriever:
        lines.append(
            f"Retriever: `{retriever.get('name', '')}` index `{retriever.get('index', '')}`."
        )
        lines.append("")
    if mode == "stub":
        lines.append(
            "Stub mode uses the reference answerer. A perfect stub score means the "
            "harness retrieved every reference passage and scored those quotes. "
            "It is not a model quality result."
        )
        lines.append("")
    lines.extend([
        f"Generator backend: `{model.get('backend', '')}`. Model: `{model.get('model', '')}`.",
        "",
        "| Question | Type | Correct | Citation | Abstention | Retrieval | Latency ms |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ])
    for row in scorecard["questions"]:
        retrieval = "n/a" if row["retrieval_hit"] is None else ("yes" if row["retrieval_hit"] else "no")
        lines.append(
            f"| `{row['question_id']}` | {row['type']} | "
            f"{'yes' if row['answer_correct'] else 'no'} | "
            f"{'yes' if row['citation_support'] else 'no'} | "
            f"{'yes' if row['abstention_correct'] else 'no'} | "
            f"{retrieval} | {row['latency_ms']} |"
        )
    lines.extend([
        "",
        "## Deterministic totals",
        "",
        f"- Answer correctness: {aggregates['answer_correct']}/{aggregates['questions']}",
        f"- Harness/model errors: {aggregates['errors']}",
        f"- Rejected model outputs: {aggregates['rejections']}",
        f"- Citation support: {aggregates['citation_support']}/{aggregates['questions']}",
        f"- Unsupported citations: {aggregates['unsupported_citation_count']}",
        f"- Abstention correctness: {aggregates['abstention_correct']}/{aggregates['questions']}",
        f"- Retrieval hit: {aggregates['retrieval_hit']}/{aggregates['retrieval_applicable']}",
        f"- Latency mean/max ms: {aggregates['latency_ms_mean']} / {aggregates['latency_ms_max']}",
        "",
        "Answer correctness requires every reference fact, and no forbidden fact, in the answer.",
        "Citation support requires each cited support span to occur in that passage and every reference span to be cited.",
        "Abstention correctness is whether the answer abstained exactly on questions whose reference is not in the record.",
        "Retrieval hit is whether a reference passage for every required fact was in the passages sent to the answerer.",
        "",
        "## Optional LLM judge",
        "",
    ])
    if scorecard["llm_judge"]["enabled"]:
        lines.append(
            "An optional LLM judge ran. Its verdicts are labeled `optional_llm_judge` "
            "and are excluded from the deterministic totals above."
        )
        for row in scorecard["questions"]:
            judgment = row.get("llm_judge") or {}
            lines.append(f"- `{row['question_id']}`: `{json.dumps(judgment, ensure_ascii=False, sort_keys=True)}`")
    else:
        lines.append("Not run. Deterministic totals do not include an LLM judge.")
    lines.extend(["", "## Misses", ""])
    misses = [
        row for row in scorecard["questions"]
        if not (row["answer_correct"] and row["citation_support"] and row["abstention_correct"])
    ]
    if not misses:
        lines.append("None.")
    for row in misses:
        lines.append(
            f"- `{row['question_id']}` missed facts `{', '.join(row['missed_facts']) or 'none'}`; "
            f"unsupported citations `{', '.join(row['unsupported_citations']) or 'none'}`; "
            f"error `{row['error'] or 'none'}`; rejection `{row['rejection'] or 'none'}`."
        )
    lines.append("")
    return "\n".join(lines)


def write_scorecard(directory: Path, scorecard: Mapping[str, Any], *, force: bool = False) -> tuple[Path, Path]:
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    json_path = (directory / "scorecard").with_suffix(".json")
    markdown_path = (directory / "scorecard").with_suffix(".md")
    if not force and (json_path.exists() or markdown_path.exists()):
        raise DefenseEvalError("scorecard already exists; choose a new directory or pass --force")
    text = json.dumps(scorecard, indent=2, ensure_ascii=False, allow_nan=False, sort_keys=True) + "\n"
    json_path.write_text(text, encoding="utf-8")
    markdown_path.write_text(render_markdown(scorecard), encoding="utf-8")
    return json_path, markdown_path


def _model_record(mode: str, profile: str | None) -> dict[str, Any]:
    if mode == "stub":
        return {"mode": "stub", "backend": "reference-stub", "model": "reference-stub", "profile": None}
    record: dict[str, Any] = {
        "mode": "configured",
        "backend": os.getenv("CASE_INTELLIGENCE_GENERATOR_BACKEND", "").strip(),
        "model": os.getenv("CASE_INTELLIGENCE_GENERATOR_MODEL", "").strip(),
        "profile": None,
    }
    if profile:
        manifest = json.loads((ROOT / "config" / "models").with_suffix(".json").read_text(encoding="utf-8"))
        matches = [
            item for item in manifest.get("models", [])
            if item.get("role") == "generator" and item.get("profile") == profile
        ]
        if len(matches) != 1:
            raise DefenseEvalError("the named generator profile is not in config/models.json")
        selected = matches[0]
        record["profile"] = {
            "name": profile,
            "model_id": selected["model_id"],
            "revision": selected["revision"],
            "license": selected["license"],
        }
    return record


def _judge_from_client(client: Any) -> Judge:
    def judge(question: Question, answer: ModelAnswer) -> dict[str, Any]:
        card = (
            f"Expect abstention: {question.expect_abstention}\n"
            + "\n".join(f"Required fact: {fact.answer_text}" for fact in question.required_facts)
            + "\n"
            + "\n".join(f"Forbidden fact: {fact}" for fact in question.forbidden_facts)
        )
        prompt = (
            "Judge the candidate against the reference card. Set answerable to true only when "
            "the candidate includes every required fact, avoids every forbidden fact, and abstains "
            "exactly when the card expects abstention. Candidate answer: "
            + " ".join(answer.text.split())[:900]
        )
        raw = client.generate(
            question=prompt,
            evidence=(EvidenceItem("S1", "reference-card", "rubric", card, "document", "reference-card"),),
        )
        return {
            "verdict_answerable": raw.get("answerable") is True,
            "raw_answerable": raw.get("answerable"),
        }
    return judge


def _read_scorecard(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise DefenseEvalError("a scorecard is not readable JSON") from exc
    if (not isinstance(payload, dict) or not isinstance(payload.get("questions"), list)
            or not payload["questions"]):
        raise DefenseEvalError("a scorecard has no question list")
    if any(not isinstance(payload.get(key), str) or not payload[key]
           for key in ("fingerprint", "suite_version")):
        raise DefenseEvalError("a scorecard has no suite fingerprint or version")
    rows: dict[str, Mapping[str, Any]] = {}
    for row in payload["questions"]:
        if (
            not isinstance(row, dict)
            or not isinstance(row.get("question_id"), str)
            or row["question_id"] in rows
            or not isinstance(row.get("type"), str)
        ):
            raise DefenseEvalError("a scorecard has a malformed question")
        for key, _label in _COMPARE_METRICS:
            if key not in row or (type(row[key]) is not bool
                                  and not (key == "retrieval_hit" and row[key] is None)):
                raise DefenseEvalError("a scorecard has a malformed score")
        rows[row["question_id"]] = row
    payload = dict(payload)
    payload["questions"] = rows
    return payload


def _metric_counts(rows: Mapping[str, Mapping[str, Any]], ids: Sequence[str], key: str) -> tuple[int, int]:
    applicable = [rows[item] for item in ids if rows[item].get(key) is not None]
    return sum(row[key] is True for row in applicable), len(applicable)


def _delta_cell(left: tuple[int, int], right: tuple[int, int]) -> str:
    return f"{left[0]}/{left[1]} -> {right[0]}/{right[1]} ({right[0] - left[0]:+d})"


def compare_scorecards(left_path: Path, right_path: Path) -> str:
    """Print per-category deltas of scorecard B minus scorecard A."""
    left = _read_scorecard(Path(left_path))
    right = _read_scorecard(Path(right_path))
    shared = sorted(set(left["questions"]) & set(right["questions"]))
    only_left = sorted(set(left["questions"]) - set(right["questions"]))
    only_right = sorted(set(right["questions"]) - set(left["questions"]))
    lines = [
        "Defense workflow comparison",
        "",
        f"A fingerprint `{left.get('fingerprint')}` version `{left.get('suite_version')}`.",
        f"B fingerprint `{right.get('fingerprint')}` version `{right.get('suite_version')}`.",
        "",
    ]
    if left.get("fingerprint") != right.get("fingerprint"):
        lines.append("Fingerprints differ. Deltas cover question ids present in both cards.")
    else:
        lines.append("Fingerprints match. Deltas are B minus A on the shared questions.")
    lines.append("")
    changed_types = [item for item in shared
                     if left["questions"][item]["type"] != right["questions"][item]["type"]]
    if changed_types:
        lines.extend(["## Changed question types", ""])
        lines.extend(f"- `{item}`: {left['questions'][item]['type']} -> {right['questions'][item]['type']}"
                     for item in changed_types)
        lines.append("")
    categories = ["overall", *sorted({card["questions"][item]["type"]
                                      for card in (left, right) for item in shared})]
    lines.append("| Category | " + " | ".join(label for _key, label in _COMPARE_METRICS) + " |")
    lines.append("| --- | " + " | ".join("---" for _ in _COMPARE_METRICS) + " |")
    for category in categories:
        left_ids = shared if category == "overall" else [
            item for item in shared if left["questions"][item]["type"] == category
        ]
        right_ids = shared if category == "overall" else [
            item for item in shared if right["questions"][item]["type"] == category
        ]
        cells = [
            _delta_cell(
                _metric_counts(left["questions"], left_ids, key),
                _metric_counts(right["questions"], right_ids, key),
            )
            for key, _label in _COMPARE_METRICS
        ]
        lines.append(f"| {category} | " + " | ".join(cells) + " |")
    lines.extend(["", "## Question flips", ""])
    flips: list[str] = []
    for item in shared:
        changes = []
        for key, label in _COMPARE_METRICS:
            before = left["questions"][item].get(key)
            after = right["questions"][item].get(key)
            if before != after:
                changes.append(f"{label} {before} -> {after}")
        if changes:
            flips.append(f"- `{item}`: " + "; ".join(changes))
    lines.append("\n".join(flips) if flips else "None.")
    if only_left or only_right:
        lines.extend([
            "",
            "Question ids only in A: " + (", ".join(f"`{item}`" for item in only_left) or "none") + ".",
            "Question ids only in B: " + (", ".join(f"`{item}`" for item in only_right) or "none") + ".",
        ])
    lines.append("")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m case_intelligence.defense_eval",
        description=(
            "Grade a configured generator, or the CI reference stub, on the "
            "versioned synthetic defense-workflow set."
        ),
    )
    parser.add_argument("--mode", choices=("stub", "configured"),
                        help="stub is the GPU-free CI answerer; configured uses CASE_INTELLIGENCE_GENERATOR_*")
    parser.add_argument("--suite", type=Path, default=None)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--top-k", type=int, default=DEFAULT_TOP_K)
    parser.add_argument("--force", action="store_true", help="replace an existing scorecard in the output directory")
    parser.add_argument("--judge", action="store_true",
                        help="configured mode only; raw LLM judgment, excluded from deterministic totals")
    parser.add_argument("--profile", choices=("portable", "quality"),
                        help="record the pinned generator profile beside the configured model name")
    parser.add_argument("--retriever", choices=("lexical", "postgres"), default="lexical",
                        help="lexical is the GPU-free database-free default; postgres runs hybrid search and the reranker")
    parser.add_argument("--compare", nargs=2, metavar=("BASELINE", "CANDIDATE"),
                        help="print per-category deltas of scorecard B minus scorecard A")
    args = parser.parse_args(argv)
    if args.compare:
        if args.mode or args.output_dir or args.judge or args.profile or args.force or args.suite or args.retriever != "lexical":
            parser.error("--compare only reads two scorecards")
        try:
            print(compare_scorecards(args.compare[0], args.compare[1]), end="")
        except (DefenseEvalError, OSError) as exc:
            print(str(exc), file=sys.stderr)
            return 2
        return 0
    if not args.mode or args.output_dir is None:
        parser.error("--mode and --output-dir are required unless --compare is set")
    if args.judge and args.mode != "configured":
        parser.error("the LLM judge runs only with --mode configured")
    if args.profile and args.mode != "configured":
        parser.error("--profile is only recorded for --mode configured")
    postgres = None
    try:
        suite = load_suite(args.suite)
        record = _model_record(args.mode, args.profile)
        judge = None
        if args.mode == "stub":
            answerer: Answerer = ReferenceStub()
        else:
            client = generator_from_environment()
            service = GroundedGenerationService(client)
            if not service.available:
                raise DefenseEvalError(
                    "the configured generator is unavailable; set CASE_INTELLIGENCE_GENERATOR_BACKEND, "
                    "CASE_INTELLIGENCE_GENERATOR_URL, and CASE_INTELLIGENCE_GENERATOR_MODEL, or use --mode stub"
                )
            answerer = ConfiguredAnswerer(service)
            if args.judge:
                judge = _judge_from_client(client)
        if args.retriever == "postgres":
            postgres = PostgresHybridRetriever(suite)
            retriever: Retriever = postgres
            retriever_record = postgres.record
        else:
            retriever = rank_passages
            retriever_record = lexical_retriever_record()
        scorecard = run_eval(
            suite, answerer, top_k=args.top_k, judge=judge, model=record,
            retriever=retriever, retriever_record=retriever_record,
        )
        json_path, markdown_path = write_scorecard(args.output_dir, scorecard, force=args.force)
        if postgres is not None:
            postgres.close()
            postgres = None
    except (DefenseEvalError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    finally:
        if postgres is not None:
            try:
                postgres.close()
            except DefenseEvalError as exc:
                print(str(exc), file=sys.stderr)
    totals = scorecard["aggregates"]
    print(f"Wrote {json_path}")
    print(f"Wrote {markdown_path}")
    print(
        f"answer {totals['answer_correct']}/{totals['questions']}; "
        f"citation {totals['citation_support']}/{totals['questions']}; "
        f"abstention {totals['abstention_correct']}/{totals['questions']}; "
        f"retrieval {totals['retrieval_hit']}/{totals['retrieval_applicable']}"
    )
    if totals["errors"]:
        print("Evaluation errors occurred; inspect the retained scorecard.", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
