"""Temporal packet coverage without models, new candidates or larger budgets."""
from dataclasses import replace

import pytest

from case_intelligence.review_bench_v2 import (
    Candidate, DeterministicEmbeddingAdapter, DeterministicReranker,
    HybridRetriever, InMemoryHybridBackend,
)
from case_intelligence.review_quality import rank_temporal_candidates


def candidate(key, text, **changes):
    return Candidate("synthetic-matter", key, key, f"{key}.txt", 1, text, **changes)


@pytest.mark.parametrize("subject,relation,cutoff,observation", [
    ("amber crate", "before", "16:45", "16:02"),
    ("violet envelope", "after", "7:20", "07:31"),
    ("copper toolbox", "before", "9:26", "09:14"),
    ("silver satchel", "after", "11:50 a.m.", "12:05 p.m."),
    ("green folder", "before", "12:10 AM", "12:01 a.m."),
    ("bronze carton", "before", "08:30:00.500", "08:30:00.250"),
])
def test_clock_observation_survives_primary_and_packet_limits(subject, relation, cutoff, observation):
    question = f"Where was the {subject} {relation} {cutoff}?"
    # Common question words and cutoff mentions used to outrank the observation.
    distractors = tuple(candidate(f"summary-{index}",
        f"The question was where the {subject} was {relation} {cutoff}. No location is recorded.")
        for index in range(20))
    source = candidate("observation", f"A witness reported carrying a {subject} at {observation}. "
                       "Its label was unreadable; the clock was unchecked and contents unknown.")
    pool = distractors + (source,)
    weak_order = DeterministicReranker().rerank(question, pool)
    assert weak_order[-1].document_id == source.document_id
    backend = InMemoryHybridBackend(pool)
    retriever = HybridRetriever(backend, DeterministicEmbeddingAdapter(), DeterministicReranker())
    rows = retriever.search("synthetic-matter", question, limit=20)
    assert len(rows) == 20
    assert rows[0].document_id == source.document_id
    assert rows[0].text == source.text
    assert rows[0].source_version_id == source.source_version_id
    assert rows[0].excerpt_digest == source.excerpt_digest
    assert len(backend.calls) == 2
    # The real workbench path has a separate eight-anchor selection boundary.
    from case_intelligence.workbench import CaseIntelligenceWorkbench, WorkbenchCitation
    citations = tuple(WorkbenchCitation(
        source_name=row.source_name, location="Page 1", excerpt=row.text, href="/synthetic",
        support_token=row.chunk_id, matter_id=row.matter_id, document_id=row.document_id,
        source_version_id=row.source_version_id, excerpt_digest=row.excerpt_digest,
        chunk_id=row.chunk_id, unit_number=1, line_start=None, line_end=None,
        evidence_kind=row.evidence_kind,
    ) for row in rows)
    packet = CaseIntelligenceWorkbench._answer_evidence_citations(
        object(), None, citations, maximum=8,
    )
    assert len(packet) == 8
    assert packet[0].excerpt == source.text


def test_temporal_ranking_requires_subject_and_direction_and_keeps_candidates_intact():
    rows = (
        candidate("cutoff", "The amber crate was discussed at 16:45."),
        candidate("unrelated", "The violet parcel left at 15:01."),
        candidate("partial-subject", "The amber curtain moved at 15:02."),
        candidate("later", "The amber crate was seen at 17:00."),
        candidate("filename-only", "The amber crate had no recorded time."),
        candidate("playback", "00:12 The amber crate was mentioned.", evidence_kind="transcript"),
        candidate("early", "A witness put the amber crate at the quay at 16:02; clock unchecked.", score=0.2),
        candidate("earlier", "A witness put the amber crate in a van at 14:20; identity uncertain.", score=0.1),
    )
    rows = rows[:4] + (replace(rows[4], source_name="15:00.txt"),) + rows[5:]
    ranked = rank_temporal_candidates("Where was the amber crate before 16:45?", rows,
                                      matter_id="synthetic-matter")
    assert ranked == rows[6:] + rows[:6]
    assert all(left is right for left, right in zip(ranked, rows[6:] + rows[:6]))


@pytest.mark.parametrize("question", [
    "Where was the amber crate?",
    "Where was the amber crate before the bell?",
    "Where was the amber crate before 16:45 and after 12:30?",
    "Was the amber crate moved not before 16:45?",
    "Before 16:45?",
    "Where was the amber crate before 99:99?",
    "Where was the amber crate before 16:45 p.m.?",
])
def test_unrecognized_or_ambiguous_temporal_question_keeps_order(question):
    rows = (candidate("summary", "The amber crate was discussed."),
            candidate("observation", "The amber crate was seen at 15:02."))
    assert rank_temporal_candidates(question, rows, matter_id="synthetic-matter") == rows


def test_temporal_ranking_rejects_foreign_matter_even_for_general_question():
    foreign = replace(candidate("other", "The amber crate at 15:02."), matter_id="other")
    with pytest.raises(RuntimeError, match="matter boundary"):
        rank_temporal_candidates("Where was the amber crate?", (foreign,), matter_id="synthetic-matter")


def test_temporal_ranking_cannot_readmit_dense_candidate_below_threshold():
    lexical = candidate("lexical", "The amber crate was discussed at 16:45.")
    dense = candidate("dense", "The amber crate left at 15:02.", score=0.1)

    class Backend:
        def lexical(self, *args):
            return (lexical,)

        def dense(self, *args):
            return (dense,)

    retriever = HybridRetriever(Backend(), DeterministicEmbeddingAdapter(), DeterministicReranker(),
                               minimum_dense_score=0.72, minimum_rerank_score=0.80)
    rows = retriever.search("synthetic-matter", "Where was the amber crate before 16:45?")
    assert [row.document_id for row in rows] == ["lexical"]
