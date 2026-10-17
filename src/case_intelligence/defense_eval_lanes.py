"""Per-lane rank diagnostics for the PostgreSQL defense-workflow retriever.

Diagnostics only. The recorders pass every call through unchanged, so the
scored search is the production search.
"""
from __future__ import annotations

from typing import Any, Mapping, Sequence

from .defense_eval import Passage, Question, tokenize


class _RecordingBackend:
    """Pass lexical and dense lane calls through unchanged and keep their output."""

    def __init__(self, backend, trace: dict[str, tuple]) -> None:
        self._backend = backend
        self._trace = trace

    def lexical(self, matter_id: str, query: str, limit: int):
        rows = tuple(self._backend.lexical(matter_id, query, limit))
        self._trace["lexical"] = rows
        return rows

    def dense(self, matter_id: str, query_vector, query: str, limit: int):
        rows = tuple(self._backend.dense(matter_id, query_vector, query, limit))
        self._trace["dense"] = rows
        return rows


class _RecordingReranker:
    """Pass rerank calls through unchanged and keep the fused input and reranked output."""

    def __init__(self, reranker, trace: dict[str, tuple]) -> None:
        self._reranker = reranker
        self._trace = trace

    @property
    def available(self) -> bool:
        return self._reranker.available

    def rerank(self, query: str, candidates):
        self._trace["fused"] = tuple(candidates)
        rows = tuple(self._reranker.rerank(query, candidates))
        self._trace["reranked"] = rows
        return rows


_NEAR_DUPLICATE_JACCARD = 0.5
DIAGNOSTIC_LANES = ("lexical", "vector", "fused", "rerank")


def _unique_documents(candidates: Sequence[Any]) -> list[str]:
    ordered: list[str] = []
    for candidate in candidates:
        if candidate.document_id not in ordered:
            ordered.append(candidate.document_id)
    return ordered


def _best_ranks(candidates: Sequence[Any]) -> dict[str, tuple[int, float]]:
    """Best 1-based rank and its score for each passage in one lane."""
    ranks: dict[str, tuple[int, float]] = {}
    for rank, candidate in enumerate(_ordered_by_document(candidates), start=1):
        ranks[candidate.document_id] = (rank, float(candidate.score))
    return ranks


def _ordered_by_document(candidates: Sequence[Any]) -> list[Any]:
    seen: set[str] = set()
    ordered: list[Any] = []
    for candidate in candidates:
        if candidate.document_id not in seen:
            seen.add(candidate.document_id)
            ordered.append(candidate)
    return ordered


def _jaccard(left: str, right: str) -> float:
    a, b = set(tokenize(left)), set(tokenize(right))
    return len(a & b) / len(a | b) if a | b else 0.0


def _lane_status(rank: int | None, top_k: int) -> str:
    if rank is None:
        return "recall"
    return "hit" if rank <= top_k else "ranking"


def lane_diagnostics_from_trace(
    question: Question,
    trace: Mapping[str, Sequence[Any]],
    *,
    top_k: int,
    rrf_k: int,
    rerank_limit: int,
    final_ids: Sequence[str],
    deep_prefix_matches: bool,
    floors: tuple[float, float] | None,
    passages: Mapping[str, Passage],
) -> dict[str, Any]:
    """Classify each reference passage from the recorded lane outputs of one search.

    Lane status is ``hit`` inside the top set, ``ranking`` when the lane had the
    passage below the top set, and ``recall`` when the lane did not return it.
    The fused order beyond the rerank limit is recomputed with the retriever's
    own reciprocal-rank formula and checked against the recorded rerank input.
    """
    lexical = tuple(trace.get("lexical", ()))
    dense = tuple(trace.get("dense", ()))
    fused_input = tuple(trace.get("fused", ()))
    reranked = tuple(trace.get("reranked", ()))
    rrf: dict[tuple[str, str, str], float] = {}
    for lane in (lexical, dense):
        for rank, item in enumerate(lane, start=1):
            key = (item.matter_id, item.document_id, item.chunk_id)
            rrf[key] = rrf.get(key, 0.0) + 1.0 / (rrf_k + rank)
    fused_keys = sorted(rrf, key=lambda key: (-rrf[key], key))
    recorded_keys = [(item.matter_id, item.document_id, item.chunk_id) for item in fused_input]
    fused_order: list[str] = []
    fused_scores: dict[str, float] = {}
    for key in fused_keys:
        if key[1] not in fused_scores:
            fused_order.append(key[1])
            fused_scores[key[1]] = rrf[key]
    lanes = {
        "lexical": _best_ranks(lexical),
        "vector": _best_ranks(dense),
        "fused": {
            document_id: (rank, fused_scores[document_id])
            for rank, document_id in enumerate(fused_order, start=1)
        },
        "rerank": _best_ranks(reranked),
    }
    lexical_ids = {item.document_id for item in lexical}
    dense_scores: dict[str, float] = {}
    for item in dense:
        dense_scores.setdefault(item.document_id, float(item.score))
    final_rank = {document_id: rank for rank, document_id in enumerate(final_ids, start=1)}
    rerank_order = [item.document_id for item in _ordered_by_document(reranked)]
    support_facts: dict[str, list[str]] = {}
    missed_facts: set[str] = set()
    top = set(final_ids[:top_k])
    for fact in question.required_facts:
        for support in fact.supports:
            support_facts.setdefault(support.passage_id, []).append(fact.fact_id)
        if not any(support.passage_id in top for support in fact.supports):
            missed_facts.add(fact.fact_id)
    near_misses = set(question.near_misses)
    rows: list[dict[str, Any]] = []
    for passage_id, fact_ids in support_facts.items():
        entry: dict[str, Any] = {"passage_id": passage_id, "fact_ids": fact_ids}
        for lane in DIAGNOSTIC_LANES:
            measured = lanes[lane].get(passage_id)
            entry[f"{lane}_rank"] = measured[0] if measured else None
            entry[f"{lane}_score"] = round(measured[1], 6) if measured else None
            entry[f"{lane}_status"] = _lane_status(measured[0] if measured else None, top_k)
        entry["final_rank"] = final_rank.get(passage_id)
        filtered = None
        if floors is not None and passage_id in lanes["rerank"]:
            filtered = passage_id not in lexical_ids and not (
                dense_scores.get(passage_id, float("-inf")) >= floors[0]
                and lanes["rerank"][passage_id][1] >= floors[1]
            )
        entry["score_floor_filtered"] = filtered
        missed = any(fact_id in missed_facts for fact_id in fact_ids)
        entry["missed"] = missed
        reason = miss_class = miss_lane = None
        crowding: list[str] = []
        if missed:
            if entry["fused_rank"] is None:
                reason, miss_class, miss_lane = "not_retrieved", "recall", "lexical+vector"
            elif entry["rerank_rank"] is None:
                reason, miss_class, miss_lane = "ranked_out", "ranking", "fused"
            elif filtered:
                reason, miss_class, miss_lane = "score_floor", "ranking", "rerank"
            else:
                rank = entry["final_rank"]
                order = list(final_ids) if rank is not None else rerank_order
                if rank is None:
                    rank = entry["rerank_rank"]
                ahead = order[: rank - 1]
                crowding = [
                    other for other in ahead
                    if other in near_misses
                    or (
                        other in passages and passage_id in passages
                        and _jaccard(passages[other].text, passages[passage_id].text)
                        >= _NEAR_DUPLICATE_JACCARD
                    )
                ]
                miss_lane = "rerank" if entry["rerank_rank"] > top_k else "post_rerank"
                miss_class = "ranking"
                reason = (
                    "crowded_by_near_duplicates"
                    if crowding and rank - len(crowding) <= top_k
                    else "ranked_out"
                )
        entry["near_duplicates_ahead"] = crowding
        entry["miss_reason"] = reason
        entry["miss_class"] = miss_class
        entry["miss_lane"] = miss_lane
        rows.append(entry)
    return {
        "top_k": top_k,
        "candidates": {
            "lexical": len(lanes["lexical"]),
            "vector": len(lanes["vector"]),
            "fused": len(lanes["fused"]),
            "rerank": len(lanes["rerank"]),
            "final_depth": len(final_ids),
        },
        "rerank_limit": rerank_limit,
        "fused_matches_rerank_input": fused_keys[:rerank_limit] == recorded_keys,
        "deep_prefix_matches": deep_prefix_matches,
        "score_floors": list(floors) if floors is not None else None,
        "passages": rows,
    }


def summarize_lane_diagnostics(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Count measured lane outcomes for every reference passage behind a missed fact."""
    misses: list[dict[str, Any]] = []
    for row in rows:
        diagnostics = row.get("lane_diagnostics") or {}
        for entry in diagnostics.get("passages", ()):
            if entry["missed"]:
                misses.append({"question_id": row["question_id"], "type": row["type"], **entry})
    by_lane = {
        lane: {
            status: sum(entry[f"{lane}_status"] == status for entry in misses)
            for status in ("hit", "ranking", "recall")
        }
        for lane in DIAGNOSTIC_LANES
    }
    by_reason: dict[str, int] = {}
    by_class: dict[str, int] = {}
    by_deciding_lane: dict[str, int] = {}
    for entry in misses:
        by_reason[entry["miss_reason"]] = by_reason.get(entry["miss_reason"], 0) + 1
        by_class[entry["miss_class"]] = by_class.get(entry["miss_class"], 0) + 1
        by_deciding_lane[entry["miss_lane"]] = by_deciding_lane.get(entry["miss_lane"], 0) + 1
    diagnosed = [row for row in rows if row.get("lane_diagnostics")]
    return {
        "method": (
            "each lane's output is recorded during the scored search; a passage behind a "
            "missed fact gets one more search at the production depth limit for its final rank"
        ),
        "near_duplicate_rule": (
            f"a named suite near miss, or token Jaccard >= {_NEAR_DUPLICATE_JACCARD} "
            "with the reference passage"
        ),
        "questions_diagnosed": len(diagnosed),
        "fused_matches_rerank_input": all(
            row["lane_diagnostics"]["fused_matches_rerank_input"] for row in diagnosed
        ),
        "deep_prefix_matches": all(
            row["lane_diagnostics"]["deep_prefix_matches"] for row in diagnosed
        ),
        "missed_passages": len(misses),
        "by_class": dict(sorted(by_class.items())),
        "by_reason": dict(sorted(by_reason.items())),
        "by_deciding_lane": dict(sorted(by_deciding_lane.items())),
        "by_lane_status": by_lane,
        "misses": misses,
    }


def _rank_cell(entry: Mapping[str, Any], lane: str) -> str:
    rank = entry.get(f"{lane}_rank")
    return "absent" if rank is None else str(rank)


def render_lane_diagnostics(summary: Mapping[str, Any]) -> list[str]:
    lines = [
        "## Per-lane rank diagnostics",
        "",
        f"Method: {summary['method']}. Near duplicate: {summary['near_duplicate_rule']}.",
        "Ranks are 1-based. `absent` means that lane did not return the passage. "
        "Rerank is before score floors; final is after floors and exact-record and temporal ordering.",
        "",
        f"- Questions diagnosed: {summary['questions_diagnosed']}",
        f"- Reference passages behind missed facts: {summary['missed_passages']}",
        f"- Recomputed fused order matches the recorded rerank input: "
        f"{'yes' if summary['fused_matches_rerank_input'] else 'no'}",
        f"- Depth search agrees with the scored top set: "
        f"{'yes' if summary['deep_prefix_matches'] else 'no'}",
        "",
    ]
    misses = summary["misses"]
    if not misses:
        lines.extend(["No missed reference passages.", ""])
        return lines
    lines.extend([
        "| Question | Passage | Lexical | Vector | Fused | Rerank | Final | Reason | Class | Lane |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ])
    for entry in misses:
        final = entry["final_rank"]
        lines.append(
            f"| `{entry['question_id']}` | `{entry['passage_id']}` | "
            + " | ".join(_rank_cell(entry, lane) for lane in DIAGNOSTIC_LANES)
            + f" | {'beyond depth' if final is None else final} | {entry['miss_reason']} | "
            f"{entry['miss_class']} | {entry['miss_lane']} |"
        )
    lines.extend([
        "",
        "| Lane | Hit | Ranking | Recall |",
        "| --- | --- | --- | --- |",
    ])
    for lane, counts in summary["by_lane_status"].items():
        lines.append(f"| {lane} | {counts['hit']} | {counts['ranking']} | {counts['recall']} |")
    lines.extend([
        "",
        "Class: " + ", ".join(f"{key} {value}" for key, value in summary["by_class"].items()) + ".",
        "Reason: " + ", ".join(f"{key} {value}" for key, value in summary["by_reason"].items()) + ".",
        "Deciding lane: "
        + ", ".join(f"{key} {value}" for key, value in summary["by_deciding_lane"].items()) + ".",
        "",
    ])
    return lines
