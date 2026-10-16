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

from .generation import (
    EvidenceItem,
    GenerationRejected,
    GenerationUnavailable,
    GroundedGenerationService,
    generator_from_environment,
)


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SUITE = ROOT / "benchmarks" / "defense-workflow" / "v1" / "suite.json"
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


def _require_keys(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    if set(value) != expected:
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
    questions = _load_questions(payload["questions"], passages)
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


def _load_questions(records: object, passages: Sequence[Passage]) -> tuple[Question, ...]:
    if not isinstance(records, list) or not records:
        raise DefenseEvalError("the suite needs questions")
    by_id = {passage.passage_id: passage for passage in passages}
    questions: list[Question] = []
    seen: set[str] = set()
    for record in records:
        if not isinstance(record, dict):
            raise DefenseEvalError("a question record is malformed")
        _require_keys(record, _QUESTION_KEYS, "question")
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
        facts = _load_facts(record["required_facts"], by_id, question_id)
        forbidden = record["forbidden_facts"]
        if not isinstance(forbidden, list) or any(not isinstance(item, str) or not item for item in forbidden):
            raise DefenseEvalError(f"{question_id} has a malformed forbidden fact")
        expects_absence = record["type"] == "not_in_record"
        if record["expect_abstention"] != expects_absence or bool(facts) == expects_absence:
            raise DefenseEvalError(f"{question_id} mixes abstention with required facts")
        questions.append(Question(
            question_id, record["matter_id"], record["type"], record["question"],
            record["expect_abstention"], facts, tuple(forbidden),
        ))
    return tuple(questions)


def _load_facts(records: object, passages: Mapping[str, Passage], question_id: str) -> tuple[Fact, ...]:
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


def _contains(haystack: str, needle: str) -> bool:
    return bool(needle) and normalize(needle) in normalize(haystack)


class ReferenceStub:
    """Quote each retrieved reference span, or abstain when the span was not retrieved.

    This is the CI answerer. It checks the harness and the lexical retriever.
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
        except (GenerationUnavailable, GenerationRejected, ValueError, OSError) as exc:
            return ModelAnswer(False, "", (), error=exc.__class__.__name__)
        citations: list[Citation] = []
        seen: set[tuple[str, str]] = set()
        claims = list(verified.claims)
        if verified.limitation is not None:
            claims.append(verified.limitation)
        for claim in claims:
            for evidence_id in claim.evidence_ids:
                key = (mapping[evidence_id], claim.text)
                if key not in seen:
                    seen.add(key)
                    citations.append(Citation(*key))
        return ModelAnswer(not verified.answerable, verified.text, tuple(citations))


def _fact_cited(fact: Fact, citations: Sequence[Citation], passages: Mapping[str, Passage]) -> bool:
    for citation in citations:
        passage = passages.get(citation.passage_id)
        if passage is None:
            continue
        for support in fact.supports:
            if citation.passage_id == support.passage_id and _contains(passage.text, support.support_text):
                return True
    return False


def _citation_allowed(citation: Citation, question: Question, passages: Mapping[str, Passage]) -> bool:
    passage = passages.get(citation.passage_id)
    if passage is None or not _contains(passage.text, citation.support_text):
        return False
    return any(
        citation.passage_id == support.passage_id and _contains(passage.text, support.support_text)
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
    abstention_correct = answer.abstained is question.expect_abstention and not answer.error
    if answer.error:
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
        "answer_text": answer.text,
        "error": answer.error,
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
    return _scorecard(suite, rows, model or {"mode": "unspecified"}, judge is not None)


def _rate(numerator: int, denominator: int) -> float | None:
    if denominator == 0:
        return None
    return round(numerator / denominator, 4)


def _scorecard(suite: Suite, rows: Sequence[Mapping[str, Any]], model: Mapping[str, Any], judged: bool) -> dict[str, Any]:
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
        f"- Citation support: {aggregates['citation_support']}/{aggregates['questions']}",
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
            f"error `{row['error'] or 'none'}`."
        )
    lines.append("")
    return "\n".join(lines)


def write_scorecard(directory: Path, scorecard: Mapping[str, Any], *, force: bool = False) -> tuple[Path, Path]:
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    json_path = directory / "scorecard.json"
    markdown_path = directory / "scorecard.md"
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
        manifest = json.loads((ROOT / "config" / "models.json").read_text(encoding="utf-8"))
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


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m case_intelligence.defense_eval",
        description=(
            "Grade a configured generator, or the CI reference stub, on the "
            "versioned synthetic defense-workflow set."
        ),
    )
    parser.add_argument("--mode", choices=("stub", "configured"), required=True,
                        help="stub is the GPU-free CI answerer; configured uses CASE_INTELLIGENCE_GENERATOR_*")
    parser.add_argument("--suite", type=Path, default=DEFAULT_SUITE)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--top-k", type=int, default=DEFAULT_TOP_K)
    parser.add_argument("--force", action="store_true", help="replace an existing scorecard in the output directory")
    parser.add_argument("--judge", action="store_true",
                        help="configured mode only; raw LLM judgment, excluded from deterministic totals")
    parser.add_argument("--profile", choices=("portable", "quality"),
                        help="record the pinned generator profile beside the configured model name")
    args = parser.parse_args(argv)
    if args.judge and args.mode != "configured":
        parser.error("the LLM judge runs only with --mode configured")
    if args.profile and args.mode != "configured":
        parser.error("--profile is only recorded for --mode configured")
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
        scorecard = run_eval(suite, answerer, top_k=args.top_k, judge=judge, model=record)
        json_path, markdown_path = write_scorecard(args.output_dir, scorecard, force=args.force)
    except (DefenseEvalError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    totals = scorecard["aggregates"]
    print(f"Wrote {json_path}")
    print(f"Wrote {markdown_path}")
    print(
        f"answer {totals['answer_correct']}/{totals['questions']}; "
        f"citation {totals['citation_support']}/{totals['questions']}; "
        f"abstention {totals['abstention_correct']}/{totals['questions']}; "
        f"retrieval {totals['retrieval_hit']}/{totals['retrieval_applicable']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
