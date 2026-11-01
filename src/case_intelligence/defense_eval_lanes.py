"""Eval-only pass-through retrieval recorders and measured miss classifications."""
from __future__ import annotations

from collections import Counter
from dataclasses import replace


LANES = ("lexical", "vector", "fused", "rerank", "final")


class RecordingBackend:
    def __init__(self, backend, trace):
        self.backend, self.trace = backend, trace

    def lexical(self, matter_id, query, limit):
        rows = self.backend.lexical(matter_id, query, limit)
        self.trace["lexical"] = tuple(rows)
        return rows

    def dense(self, matter_id, vector, query, limit):
        rows = self.backend.dense(matter_id, vector, query, limit)
        self.trace["vector"] = tuple(rows)
        return rows


class RecordingReranker:
    def __init__(self, reranker, trace):
        self.reranker, self.trace = reranker, trace

    @property
    def available(self):
        return self.reranker.available

    def rerank(self, query, candidates):
        self.trace["fused"] = tuple(candidates)
        rows = self.reranker.rerank(query, candidates)
        self.trace["rerank"] = tuple(rows)
        return rows


def _key(item):
    return item.matter_id, item.document_id, item.chunk_id


def _ranks(rows):
    return {item.document_id: (rank, item.score) for rank, item in enumerate(rows, 1)}


def diagnose_lanes(adapter, question, retrieved, top_k, *, error_type, tokenize):
    from .review_budget import DEFAULT_REVIEW_BUDGET
    last = adapter._last_search
    if last is None or last[:3] != (question.matter_id, question.text, top_k):
        raise error_type("diagnostics require the question's scored search")
    _, query, _, trace, hits = last
    retriever = adapter._retriever
    supports = {s.passage_id for f in question.required_facts for s in f.supports}
    final = hits
    if supports - {p.passage_id for p in retrieved}:
        try:
            final = retriever.search(adapter._database_ids[question.matter_id], query,
                                     DEFAULT_REVIEW_BUDGET.primary_candidates)
        except Exception as exc:
            raise error_type("PostgreSQL diagnostic search failed") from exc
        if final[:top_k] != hits:
            raise error_type("diagnostic depth search changed the scored prefix")
    scores, candidates = {}, {}
    for name in ("lexical", "vector"):
        for rank, item in enumerate(trace.get(name, ()), 1):
            key = _key(item)
            candidates[key] = item
            scores[key] = scores.get(key, 0.0) + 1.0 / (retriever.rrf_k + rank)
    fused = tuple(replace(candidates[key], score=scores[key])
                  for key in sorted(scores, key=lambda key: (-scores[key], key)))
    if fused[:retriever.rerank_limit] != trace.get("fused", ()):
        raise error_type("diagnostic fusion differs from recorded rerank input")
    lane_rows = {**trace, "fused": fused, "final": final}
    ranks = {name: _ranks(lane_rows.get(name, ())) for name in LANES}
    top = {item.document_id for item in hits}
    missed_facts = {f.fact_id for f in question.required_facts
                    if not any(s.passage_id in top for s in f.supports)}
    passages = {p.passage_id: p for p in adapter.suite.passages}
    entries = []
    for passage_id in sorted(supports):
        facts = [f.fact_id for f in question.required_facts
                 if any(s.passage_id == passage_id for s in f.supports)]
        entry = dict(passage_id=passage_id, fact_ids=facts, missed=bool(missed_facts.intersection(facts)))
        for name in LANES:
            rank, score = ranks[name].get(passage_id, (None, None))
            entry.update({name + "_rank": rank, name + "_score": score,
                          name + "_status": "recall" if rank is None else
                          ("hit" if rank <= top_k else "ranking")})
        filtered = False
        if retriever.minimum_dense_score is not None and passage_id in ranks["rerank"]:
            filtered = passage_id not in ranks["lexical"] and not (
                ranks["vector"].get(passage_id, (None, float("-inf")))[1] >= retriever.minimum_dense_score
                and ranks["rerank"][passage_id][1] >= retriever.minimum_rerank_score)
        reason = lane = category = None
        crowding = []
        if entry["missed"]:
            category = "ranking"
            if entry["fused_rank"] is None:
                reason, lane, category = "not_retrieved", "lexical+vector", "recall"
            elif entry["rerank_rank"] is None:
                reason, lane = "ranked_out", "fused"
            elif filtered:
                reason, lane = "score_floor", "rerank"
            elif entry["final_rank"] is None:
                reason, lane = "beyond_depth", "post_rerank"
            else:
                rank = entry["final_rank"]
                reference = set(tokenize(passages[passage_id].text))
                for other in final[:rank - 1]:
                    tokens = set(tokenize(other.text))
                    overlap = len(tokens & reference) / len(tokens | reference) if tokens | reference else 0
                    if other.document_id in question.near_misses or overlap >= 0.5:
                        crowding.append(other.document_id)
                if crowding and rank - len(crowding) <= top_k:
                    reason, lane = "crowded_by_near_duplicates", "post_rerank"
                else:
                    reason = "ranked_out"
                    lane = "rerank" if entry["rerank_rank"] > top_k else "post_rerank"
        entry.update(miss_reason=reason, miss_lane=lane, miss_class=category,
                     score_floor_filtered=filtered, near_duplicates_ahead=crowding)
        entries.append(entry)
    return dict(top_k=top_k, final_depth=len(final), passages=entries)


def summarize_lane_diagnostics(rows):
    misses = [dict(question_id=row["question_id"], **entry) for row in rows
              for entry in (row.get("lane_diagnostics") or {}).get("passages", ()) if entry["missed"]]
    return dict(missed_passages=len(misses), misses=misses,
                by_reason=dict(Counter(e["miss_reason"] for e in misses)),
                by_deciding_lane=dict(Counter(e["miss_lane"] for e in misses)),
                by_lane_status={name: {status: sum(e[name + "_status"] == status for e in misses)
                                      for status in ("hit", "ranking", "recall")} for name in LANES})


def render_lane_diagnostics(summary):
    lines = ["## Per-lane rank diagnostics", "",
             "Ranks are 1-based; absent means not observed at the measured depth. "
             "Rerank precedes floors; final follows floors and reorder.", "",
             "| Question | Passage | Lexical | Vector | Fused | Rerank | Final | Reason | Lane |",
             "| --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for entry in summary["misses"]:
        ranks = " | ".join(str(entry.get(name + "_rank") or "absent") for name in LANES)
        lines.append(f"| `{entry['question_id']}` | `{entry['passage_id']}` | {ranks} | "
                     f"{entry['miss_reason']} | {entry['miss_lane']} |")
    lines += ["", "| Lane | Hit | Ranking | Recall |", "| --- | --- | --- | --- |"]
    for name, counts in summary["by_lane_status"].items():
        lines.append(f"| {name} | {counts['hit']} | {counts['ranking']} | {counts['recall']} |")
    lines += ["", "Reasons: " + ", ".join(f"{k} {v}" for k, v in summary["by_reason"].items()), ""]
    return lines


