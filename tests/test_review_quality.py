from __future__ import annotations

import re
from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from case_intelligence.generation import (
    EvidenceItem,
    GenerationGroundingRejected,
    GroundedGenerationService,
    VerifiedAnswer,
    _prompt,
)
from case_intelligence.review_bench_v2 import (
    Candidate,
    DeterministicEmbeddingAdapter,
    HybridRetriever,
)
from case_intelligence.review_quality import (
    QUALITY_EVALUATION_CASES,
    QUALITY_EVALUATION_FINGERPRINT,
    answer_advances_objective,
    broad_summary_queries,
    classify_question,
    filter_broad_summary_evidence,
    modality_coverage,
    prioritize_evidence_kinds,
    rank_exact_record_candidates,
    research_step_question,
    research_synthesis_question,
    quality_evaluation_fingerprint,
)
from case_intelligence.workbench import (
    CaseIntelligenceWorkbench,
    WorkbenchCitation,
    _focused_answer_scope,
)
from case_intelligence.workspace_store import MatterReadinessRecord


@dataclass(frozen=True)
class _Citation:
    support_token: str
    document_id: str
    evidence_kind: str
    excerpt: str
    location: str


def _workbench_citation(
    token: str,
    document: str,
    kind: str,
    excerpt: str,
    location: str,
) -> WorkbenchCitation:
    return WorkbenchCitation(
        source_name=f"Generated {document}",
        location=location,
        excerpt=excerpt,
        href=f"/generated?support={token}",
        support_token=token,
        matter_id="matter-quality",
        document_id=document,
        source_version_id="quality-v1",
        excerpt_digest=(token * 64)[:64],
        chunk_id=f"chunk-{token}",
        unit_number=1,
        line_start=None,
        line_end=None,
        evidence_kind=kind,
    )


def _candidate(
    document: str,
    chunk: str,
    name: str,
    text: str,
    score: float,
    *,
    matter: str = "matter-quality",
) -> Candidate:
    return Candidate(
        matter,
        document,
        chunk,
        name,
        1,
        text,
        score,
        source_version_id="quality-v1",
        excerpt_digest=(chunk * 64)[:64],
    )


def test_exact_record_rescue_prefers_precise_contemporaneous_machine_record() -> None:
    later_summary = _candidate(
        "summary",
        "a",
        "Later narrative summary.pdf",
        "A later overview says event EVT-4821 happened at about 08:40.",
        0.96,
    )
    machine_record = _candidate(
        "event-log",
        "b",
        "Access control event log.csv",
        "System event EVT-4821 | accepted | 08:42:17 | reader 4.",
        0.31,
    )
    ranked = rank_exact_record_candidates(
        "At what exact time did system event EVT-4821 occur?",
        (later_summary, machine_record),
        matter_id="matter-quality",
    )
    assert [item.document_id for item in ranked] == ["event-log", "summary"]
    assert ranked[0].score == 0.31


def test_exact_record_rescue_is_matter_fenced_and_does_not_reorder_general_queries() -> None:
    first = _candidate("first", "a", "Interview.pdf", "A witness discussed the package.", 0.9)
    second = _candidate("second", "b", "Event log.csv", "A system logged the package.", 0.4)
    assert rank_exact_record_candidates(
        "What was said about the package?", (first, second), matter_id="matter-quality"
    ) == (first, second)
    with pytest.raises(RuntimeError, match="matter boundary"):
        rank_exact_record_candidates(
            "What exact time was recorded?",
            (first, _candidate("foreign", "c", "Log.csv", "08:42:17", 1.0, matter="other")),
            matter_id="matter-quality",
        )


def test_exact_record_policy_never_promotes_an_unrelated_precise_log() -> None:
    meeting_note = _candidate(
        "meeting-note",
        "a",
        "Contemporaneous meeting note.txt",
        "The meeting occurred at 10:15.",
        0.95,
    )
    unrelated_log = _candidate(
        "access-log",
        "b",
        "Access control event log.csv",
        "A badge event was accepted at 08:42:17.",
        0.20,
    )
    ranked = rank_exact_record_candidates(
        "At what exact time did the meeting occur?",
        (meeting_note, unrelated_log),
        matter_id="matter-quality",
    )
    assert ranked == (meeting_note, unrelated_log)


def test_hybrid_retrieval_rescues_exact_record_after_a_weak_model_rerank() -> None:
    summary = Candidate(
        "matter-quality",
        "summary",
        "summary-chunk",
        "Later narrative summary.pdf",
        3,
        "A later overview says event EVT-4821 happened at about 08:40.",
        0.0,
    )
    event = Candidate(
        "matter-quality",
        "event",
        "event-chunk",
        "Access control event log.csv",
        7,
        "System event EVT-4821 | accepted | 08:42:17 | reader 4.",
        0.0,
    )

    class Backend:
        def lexical(self, matter_id, query, limit):
            return summary, event

        def dense(self, matter_id, query_vector, query, limit):
            return summary, event

    class WeakReranker:
        available = True

        def rerank(self, query, candidates):
            by_document = {item.document_id: item for item in candidates}
            return by_document["summary"], by_document["event"]

    results = HybridRetriever(
        Backend(), DeterministicEmbeddingAdapter(), WeakReranker()
    ).search(
        "matter-quality",
        "At what exact time did system event EVT-4821 occur?",
        limit=2,
    )
    assert [item.document_id for item in results] == ["event", "summary"]
    assert results[0].citation == "Page 7"


def test_explicit_written_and_spoken_request_prioritizes_both_without_changing_locators() -> None:
    intent = classify_question(
        "Using both the written records and the spoken recording, what identifier was stated?"
    )
    assert intent.required_evidence_kinds == ("document", "transcript")
    citations = (
        _Citation("d1", "doc-a", "document", "Written identifier ZX-41.", "Page 4"),
        _Citation("d2", "doc-b", "document", "Later written note.", "Page 8"),
        _Citation("t1", "media-a", "transcript", "I said ZX-41.", "00:31–00:34"),
    )
    selected = prioritize_evidence_kinds(
        citations, maximum=2, required_kinds=intent.required_evidence_kinds
    )
    assert [(item.evidence_kind, item.location) for item in selected] == [
        ("document", "Page 4"),
        ("transcript", "00:31–00:34"),
    ]
    assert classify_question(
        "Using the written report, what was said about the identifier?"
    ).required_evidence_kinds == ()
    for wording in (
        "Compare the written report with the recorded interview.",
        "Use the email and the phone call to identify the reference.",
        "What do the documents and witness testimony say?",
    ):
        assert classify_question(wording).required_evidence_kinds == (
            "document",
            "transcript",
        )


def test_explicit_modality_exclusion_is_directional_and_does_not_capture_factual_negation() -> None:
    for wording in (
        "Answer from the written report, not the transcript.",
        "Use the report rather than the recording.",
        "Do not use the transcript; answer from the notes.",
    ):
        intent = classify_question(wording)
        assert intent.required_evidence_kinds == ("document",)
        assert intent.excluded_evidence_kinds == ("transcript",)

    reverse = classify_question("Use the transcript, not the written report.")
    assert reverse.required_evidence_kinds == ("transcript",)
    assert reverse.excluded_evidence_kinds == ("document",)

    for wording in (
        "Which witness did not receive the written report?",
        "According to the written report, who did not receive the transcript, and why?",
        "According to the written report, who did not compare the transcript, and why?",
    ):
        factual = classify_question(wording)
        assert factual.required_evidence_kinds == ()
        assert factual.excluded_evidence_kinds == ()

    inclusive = classify_question(
        "Use not only the transcript but also the written report."
    )
    assert inclusive.required_evidence_kinds == ("document", "transcript")
    assert inclusive.excluded_evidence_kinds == ()


def test_modality_coverage_distinguishes_complete_and_clear_partial_result() -> None:
    question = "Compare both the written report and what was said in the recording."
    evidence = {
        "S1": _Citation("d1", "doc-a", "document", "Written identifier ZX-41.", "Page 4"),
        "S2": _Citation("t1", "media-a", "transcript", "I said ZX-41.", "00:31–00:34"),
    }
    complete = modality_coverage(question, evidence, ("S1", "S2"))
    assert complete["mode"] == "complete"
    assert complete["used_evidence_kinds"] == ["written", "spoken"]
    partial = modality_coverage(question, evidence, ("S1",))
    assert partial["mode"] == "partial"
    assert partial["missing_evidence_kinds"] == ["spoken"]
    assert "partial" in str(partial["notice"]).casefold()
    assert "spoken" in str(partial["notice"]).casefold()


def test_generation_repairs_an_explicit_dual_modality_answer_to_use_both() -> None:
    evidence = (
        EvidenceItem("S1", "Generated report.pdf", "Page 4", "The written identifier is ZX-41."),
        EvidenceItem(
            "S2",
            "Generated interview.wav",
            "00:31–00:34",
            "The spoken identifier is ZX-41.",
            "transcript",
        ),
    )

    class DualModalGenerator:
        available = True

        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        def generate(self, **kwargs):
            self.calls.append(kwargs)
            claims = [
                {"text": "The written identifier is ZX-41.", "evidence_ids": ["S1"]}
            ]
            if kwargs["grounding_repair"]:
                claims.append(
                    {
                        "text": (
                            "The machine transcript appears to say that the spoken "
                            "identifier is ZX-41."
                        ),
                        "evidence_ids": ["S2"],
                    }
                )
            return {
                "answerable": True,
                "claims": claims,
                "limitation": None,
                "missing_information": "",
            }

    client = DualModalGenerator()
    answer = GroundedGenerationService(client).answer(
        "Using both the written report and spoken recording, what identifier appears?",
        evidence,
    )
    assert answer.used_evidence_ids == ("S1", "S2")
    assert [call["grounding_repair"] for call in client.calls] == [False, True]


def test_document_claim_cannot_masquerade_as_machine_transcript_statement() -> None:
    class WrongKindGenerator:
        available = True

        def generate(self, **kwargs):
            return {
                "answerable": True,
                "claims": [
                    {
                        "text": (
                            "The machine transcript appears to say that the written "
                            "identifier is ZX-41."
                        ),
                        "evidence_ids": ["S1"],
                    }
                ],
                "limitation": None,
                "missing_information": "",
            }

    with pytest.raises(GenerationGroundingRejected):
        GroundedGenerationService(WrongKindGenerator()).answer(
            "What identifier is in the report?",
            (EvidenceItem("S1", "Generated report.pdf", "Page 4", "The written identifier is ZX-41."),),
        )

    for misleading_text in (
        "The transcript says the written identifier is ZX-41.",
        "According to the transcript, the written identifier is ZX-41.",
        "Audio transcript evidence indicates the written identifier is ZX-41.",
    ):
        class MisleadingTranscriptGenerator:
            available = True

            def generate(self, **kwargs):
                return {
                    "answerable": True,
                    "claims": [
                        {"text": misleading_text, "evidence_ids": ["S1"]}
                    ],
                    "limitation": None,
                    "missing_information": "",
                }

        with pytest.raises(GenerationGroundingRejected):
            GroundedGenerationService(MisleadingTranscriptGenerator()).answer(
                "What identifier is in the report?",
                (
                    EvidenceItem(
                        "S1",
                        "Generated report.pdf",
                        "Page 4",
                        "The written identifier is ZX-41.",
                    ),
                ),
            )


def test_mixed_kind_claim_cannot_swap_document_and_transcript_attribution() -> None:
    class SwappedAttributionGenerator:
        available = True

        def generate(self, **kwargs):
            return {
                "answerable": True,
                "claims": [
                    {
                        "text": (
                            "The report lists ZX-41, while the machine transcript "
                            "says QP-77."
                        ),
                        "evidence_ids": ["S1", "S2"],
                    }
                ],
                "limitation": None,
                "missing_information": "",
            }

    evidence = (
        EvidenceItem(
            "S1",
            "Generated report.pdf",
            "Page 4",
            "The report lists QP-77.",
        ),
        EvidenceItem(
            "S2",
            "Generated interview.wav",
            "00:31–00:34",
            "The spoken identifier is ZX-41.",
            "transcript",
        ),
    )

    with pytest.raises(GenerationGroundingRejected):
        GroundedGenerationService(SwappedAttributionGenerator()).answer(
            "Using the written report and spoken recording, what identifiers appear?",
            evidence,
        )


def test_exact_time_objective_gets_one_repair_instead_of_an_adjacent_finding() -> None:
    evidence = (
        EvidenceItem(
            "S1",
            "Generated event log.csv",
            "Line 7",
            "System event EVT-4821 was accepted at 08:42:17 by reader 4.",
        ),
    )

    class ObjectiveGenerator:
        available = True

        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        def generate(self, **kwargs):
            self.calls.append(kwargs)
            text = (
                "System event EVT-4821 was accepted at 08:42:17 by reader 4."
                if kwargs["grounding_repair"]
                else "System event EVT-4821 was accepted by reader 4."
            )
            return {
                "answerable": True,
                "claims": [{"text": text, "evidence_ids": ["S1"]}],
                "limitation": None,
                "missing_information": "",
            }

    client = ObjectiveGenerator()
    answer = GroundedGenerationService(client).answer(
        "At what exact time did system event EVT-4821 occur?",
        evidence,
    )
    assert answer_advances_objective(
        "At what exact time did system event EVT-4821 occur?", answer.text
    )
    assert "08:42:17" in answer.text
    assert [call["grounding_repair"] for call in client.calls] == [False, True]

    class StillAdjacent(ObjectiveGenerator):
        def generate(self, **kwargs):
            self.calls.append(kwargs)
            return {
                "answerable": True,
                "claims": [
                    {
                        "text": "System event EVT-4821 was accepted by reader 4.",
                        "evidence_ids": ["S1"],
                    }
                ],
                "limitation": None,
                "missing_information": "",
            }

    with pytest.raises(GenerationGroundingRejected, match="exact objective"):
        GroundedGenerationService(StillAdjacent()).answer(
            "At what exact time did system event EVT-4821 occur?",
            evidence,
        )


def test_exact_time_objective_is_enforced_after_a_grounding_repair() -> None:
    evidence = (
        EvidenceItem(
            "S1",
            "Generated event log.csv",
            "Line 7",
            "System event EVT-4821 was accepted at 08:42:17 by reader 4.",
        ),
    )

    class GroundingThenAdjacent:
        available = True

        def generate(self, **kwargs):
            if not kwargs["grounding_repair"]:
                text = "A neighboring record may concern EVT-4821."
            else:
                text = "System event EVT-4821 was accepted by reader 4."
            return {
                "answerable": True,
                "claims": [{"text": text, "evidence_ids": ["S1"]}],
                "limitation": None,
                "missing_information": "",
            }

    with pytest.raises(GenerationGroundingRejected, match="exact objective"):
        GroundedGenerationService(GroundingThenAdjacent()).answer(
            "At what exact time did system event EVT-4821 occur?",
            evidence,
        )


def test_exact_time_must_be_associated_with_each_requested_identifier() -> None:
    assert not answer_advances_objective(
        "At what exact time did system event EVT-4821 occur?",
        (
            "System event EVT-4821 was accepted, but no time is stated.\n"
            "Unrelated system event EVT-9999 occurred at 07:15:00."
        ),
    )
    assert answer_advances_objective(
        "At what exact time did system event EVT-4821 occur?",
        "System event EVT-4821 occurred at 08:42:17.",
    )
    assert not answer_advances_objective(
        "At what exact time did system event EVT-4821 occur?",
        "EVT-4821 has no stated time, while EVT-9999 occurred at 07:15:00.",
    )


def test_exact_time_distinguishes_source_references_from_timed_subjects() -> None:
    question = "Using report DOC-1234, at what exact time did event EVT-4821 occur?"
    for source_reference in (
        "Using report DOC-1234",
        "From exhibit EX-77",
        "According to the document DOC-1234",
        "Per source SRC-9",
    ):
        assert answer_advances_objective(
            f"{source_reference}, at what exact time did event EVT-4821 occur?",
            "Event EVT-4821 occurred at 08:42:17.",
        )
    assert not answer_advances_objective(
        question,
        (
            "Report DOC-1234 was received at 07:15:00. "
            "Event EVT-4821 occurred, but its time was not stated."
        ),
    )
    two_events = (
        "Using report DOC-1234, report the exact time for events "
        "EVT-4821 and EVT-4822."
    )
    assert not answer_advances_objective(
        two_events,
        "EVT-4821 occurred at 08:42:17. EVT-4822 has no stated time.",
    )
    assert answer_advances_objective(
        two_events,
        "EVT-4821 occurred at 08:42:17. EVT-4822 occurred at 08:43:09.",
    )
    source_is_subject = "At what exact time was report DOC-1234 created?"
    assert not answer_advances_objective(
        source_is_subject,
        "Report DOC-1234 has no stated creation time. EVT-4821 occurred at 08:42:17.",
    )
    assert answer_advances_objective(
        source_is_subject, "Report DOC-1234 was created at 07:15:00."
    )
    dual_role = (
        "Using report DOC-1234, report the exact time DOC-1234 was created "
        "and event EVT-4821 occurred."
    )
    assert not answer_advances_objective(
        dual_role,
        "DOC-1234 has no stated creation time. EVT-4821 occurred at 08:42:17.",
    )
    assert answer_advances_objective(
        dual_role,
        "DOC-1234 was created at 07:15:00. EVT-4821 occurred at 08:42:17.",
    )
    repeated_event = (
        "Using report EVT-4821, report the exact time events EVT-4821 and "
        "EVT-4822 occurred."
    )
    assert not answer_advances_objective(
        repeated_event,
        "Report EVT-4821 was reviewed, but the event time was not stated. "
        "Event EVT-4822 occurred at 07:15:00.",
    )


def test_source_reference_exact_time_accepts_a_grounded_first_draft() -> None:
    evidence = (
        EvidenceItem(
            "S1",
            "Generated report DOC-1234.pdf",
            "Page 4",
            "Event EVT-4821 occurred at 08:42:17.",
        ),
    )

    class SourceReferencedTimeGenerator:
        available = True

        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        def generate(self, **kwargs):
            self.calls.append(kwargs)
            return {
                "answerable": True,
                "claims": [
                    {
                        "text": "Event EVT-4821 occurred at 08:42:17.",
                        "evidence_ids": ["S1"],
                    }
                ],
                "limitation": None,
                "missing_information": "",
            }

    generator = SourceReferencedTimeGenerator()
    answer = GroundedGenerationService(generator).answer(
        "Using report DOC-1234, at what exact time did event EVT-4821 occur?",
        evidence,
    )
    assert answer.text.endswith("Event EVT-4821 occurred at 08:42:17.")
    assert [call["grounding_repair"] for call in generator.calls] == [False]


def test_broad_summary_uses_multi_pass_queries_and_suppresses_boilerplate_only() -> None:
    intent = classify_question("Give me a broad summary of this matter's evidence.")
    assert intent.broad_summary is True
    queries = broad_summary_queries("Give me a broad summary of this matter's evidence.")
    assert len(queries) >= 4
    assert len({query.casefold() for query in queries}) == len(queries)
    rows = (
        _Citation(
            "b1",
            "cover",
            "document",
            "This generated demonstration is synthetic and is not legal advice.",
            "Page 1",
        ),
        _Citation(
            "s1",
            "record",
            "document",
            "The system register records transfer ZX-41 at 08:42:17.",
            "Page 2",
        ),
    )
    assert filter_broad_summary_evidence(rows) == (rows[1],)
    for wording in (
        "Give me a case overview.",
        "Provide a summary of all sources.",
        "What are the key facts across the matter?",
        "Give me an overview of all the evidence.",
    ):
        assert classify_question(wording).broad_summary is True


def test_broad_summary_repairs_a_one_source_draft_toward_source_diversity() -> None:
    evidence = tuple(
        EvidenceItem(
            f"S{index}",
            f"Generated source {index}.txt",
            f"Line {index}",
            f"Generated source {index} records substantive event {index}.",
        )
        for index in range(1, 4)
    )

    class BroadGenerator:
        available = True

        def __init__(self) -> None:
            self.calls = 0

        def generate(self, **kwargs):
            self.calls += 1
            selected = kwargs["evidence"] if kwargs["grounding_repair"] else kwargs["evidence"][:1]
            return {
                "answerable": True,
                "claims": [
                    {"text": item.excerpt, "evidence_ids": [item.evidence_id]}
                    for item in selected
                ],
                "limitation": None,
                "missing_information": "",
            }

    generator = BroadGenerator()
    answer = GroundedGenerationService(generator).answer(
        "Give me a case overview.", evidence
    )
    assert generator.calls == 2
    assert answer.used_evidence_ids == ("S1", "S2", "S3")


def test_workbench_broad_answer_search_merges_facets_and_drops_disclaimer_only() -> None:
    boilerplate = _workbench_citation(
        "b" * 40,
        "cover",
        "document",
        "This generated demonstration is synthetic and is not legal advice.",
        "Page 1",
    )

    class SearchHarness:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def search(self, matter, query, **kwargs):
            self.calls.append(query)
            ordinal = len(self.calls)
            return (
                boilerplate,
                _workbench_citation(
                    f"{ordinal:040x}",
                    f"record-{ordinal}",
                    "document",
                    f"System register {ordinal} records a substantive event at 08:4{ordinal}.",
                    f"Page {ordinal + 1}",
                ),
            )

    harness = SearchHarness()
    question = "Give me a broad summary of this matter's evidence."
    rows = CaseIntelligenceWorkbench._answer_search(
        harness,
        SimpleNamespace(matter_id="matter-quality"),
        question,
        question,
    )
    assert len(harness.calls) >= 4
    assert all(row.document_id != "cover" for row in rows)
    assert len({row.document_id for row in rows}) == len(rows)


def test_workbench_joint_request_runs_a_bounded_missing_modality_search() -> None:
    written = _workbench_citation(
        "d" * 40,
        "report",
        "document",
        "The written identifier is ZX-41.",
        "Page 4",
    )
    spoken = _workbench_citation(
        "e" * 40,
        "media",
        "transcript",
        "The spoken identifier is ZX-41.",
        "00:31–00:34",
    )

    class SearchHarness:
        def __init__(self) -> None:
            self.scopes: list[frozenset[str] | None] = []

        def search(self, matter, query, *, document_ids=None, **kwargs):
            self.scopes.append(document_ids)
            return (spoken,) if document_ids == frozenset({"media"}) else (written,)

        def source_store(self, matter):
            return SimpleNamespace(
                ready_documents=lambda: (
                    SimpleNamespace(document_id="report", media_type="application/pdf"),
                    SimpleNamespace(document_id="media", media_type="audio/wav"),
                )
            )

    harness = SearchHarness()
    question = "Using both the written report and spoken recording, what identifier appears?"
    rows = CaseIntelligenceWorkbench._answer_search(
        harness,
        SimpleNamespace(matter_id="matter-quality"),
        question,
        question,
    )
    assert harness.scopes == [None, frozenset({"media"})]
    assert [(row.evidence_kind, row.location) for row in rows] == [
        ("document", "Page 4"),
        ("transcript", "00:31–00:34"),
    ]


def test_workbench_excluded_modality_is_dropped_before_bounded_backfill() -> None:
    written = _workbench_citation(
        "f" * 40,
        "report",
        "document",
        "The written identifier is ZX-41.",
        "Page 4",
    )
    spoken = _workbench_citation(
        "a" * 40,
        "media",
        "transcript",
        "The spoken identifier is ZX-41.",
        "00:31–00:34",
    )

    class SearchHarness:
        def __init__(self) -> None:
            self.scopes: list[frozenset[str] | None] = []

        def search(self, matter, query, *, document_ids=None, **kwargs):
            self.scopes.append(document_ids)
            return (written,) if document_ids == frozenset({"report"}) else (spoken,)

        def source_store(self, matter):
            return SimpleNamespace(
                ready_documents=lambda: (
                    SimpleNamespace(document_id="report", media_type="application/pdf"),
                    SimpleNamespace(document_id="media", media_type="audio/wav"),
                )
            )

    harness = SearchHarness()
    question = "Answer from the written report, not the transcript."
    rows = CaseIntelligenceWorkbench._answer_search(
        harness,
        SimpleNamespace(matter_id="matter-quality"),
        question,
        question,
    )
    assert harness.scopes == [None, frozenset({"report"})]
    assert [(row.evidence_kind, row.location) for row in rows] == [
        ("document", "Page 4"),
    ]


def test_factual_modality_mentions_do_not_trigger_source_kind_backfill() -> None:
    written = _workbench_citation(
        "b" * 40,
        "report",
        "document",
        "The generated report states Pat did not receive the transcript.",
        "Page 6",
    )
    spoken = _workbench_citation(
        "c" * 40,
        "media",
        "transcript",
        "Pat asks why the file was missing.",
        "00:42–00:46",
    )

    class SearchHarness:
        def __init__(self) -> None:
            self.scopes: list[frozenset[str] | None] = []

        def search(self, matter, query, *, document_ids=None, **kwargs):
            self.scopes.append(document_ids)
            return (spoken,) if document_ids == frozenset({"media"}) else (written,)

        def source_store(self, matter):
            return SimpleNamespace(
                ready_documents=lambda: (
                    SimpleNamespace(document_id="report", media_type="application/pdf"),
                    SimpleNamespace(document_id="media", media_type="audio/wav"),
                )
            )

    harness = SearchHarness()
    question = (
        "According to the written report, who did not receive the transcript, and why?"
    )
    rows = CaseIntelligenceWorkbench._answer_search(
        harness,
        SimpleNamespace(matter_id="matter-quality"),
        question,
        question,
    )
    assert harness.scopes == [None]
    assert rows == (written,)

    class RecordingGenerator:
        available = True

        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        def generate(self, **kwargs):
            self.calls.append(kwargs)
            return {
                "answerable": True,
                "claims": [
                    {
                        "text": written.excerpt,
                        "evidence_ids": ["S1"],
                    }
                ],
                "limitation": None,
                "missing_information": "",
            }

    generator = RecordingGenerator()
    answer = GroundedGenerationService(generator).answer(
        question,
        (
            EvidenceItem(
                "S1",
                written.source_name,
                written.location,
                written.excerpt,
                written.evidence_kind,
            ),
        ),
    )
    assert answer.used_evidence_ids == ("S1",)
    assert len(generator.calls) == 1
    assert {item.evidence_kind for item in generator.calls[0]["evidence"]} == {
        "document"
    }


def test_generation_excludes_forbidden_modality_before_the_model_boundary() -> None:
    evidence = (
        EvidenceItem(
            "S1",
            "Generated interview.wav",
            "00:31–00:34",
            "The spoken identifier is ZX-41.",
            "transcript",
        ),
        EvidenceItem(
            "S2",
            "Generated report.pdf",
            "Page 4",
            "The written identifier is ZX-41.",
        ),
    )

    class RecordingDocumentGenerator:
        available = True

        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        def generate(self, **kwargs):
            self.calls.append(kwargs)
            return {
                "answerable": True,
                "claims": [
                    {
                        "text": "The written identifier is ZX-41.",
                        "evidence_ids": ["S2"],
                    }
                ],
                "limitation": None,
                "missing_information": "",
            }

    client = RecordingDocumentGenerator()
    question = "Answer from the written report, not the transcript."

    answer = GroundedGenerationService(client).answer(question, evidence)

    assert answer.used_evidence_ids == ("S2",)
    assert len(client.calls) == 1
    assert client.calls[0]["question"] == question
    assert [item.evidence_id for item in client.calls[0]["evidence"]] == ["S2"]
    system_prompt, user_prompt = _prompt(
        question,
        client.calls[0]["evidence"],
        (),
    )
    assert "expressly excluded machine transcript evidence" in system_prompt
    assert "Generated interview.wav" not in user_prompt
    assert "Generated report.pdf" in user_prompt


def test_broad_summary_scope_discloses_sampled_source_diversity() -> None:
    readiness = MatterReadinessRecord(
        "matter-quality",
        "ready",
        9,
        9,
        9,
        9,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        100,
        "2026-08-31T12:00:00Z",
    )
    evidence = {
        "S1": _workbench_citation("1" * 40, "one", "document", "Event one.", "Page 1"),
        "S2": _workbench_citation("2" * 40, "two", "document", "Event two.", "Page 2"),
    }
    answer = VerifiedAnswer(
        True,
        "Supported orientation:",
        (),
        None,
        "",
        ("S1", "S2"),
        True,
        1,
    )
    scope = _focused_answer_scope(
        "Give me a broad summary of this matter's evidence.",
        readiness,
        answer,
        evidence,
    )
    assert scope["mode"] == "broader_orientation"
    assert scope["candidate_source_count"] == 2
    assert "not an every-source review" in str(scope["notice"])

    focused = VerifiedAnswer(
        True,
        "Focused result:",
        (),
        None,
        "",
        ("S1",),
        True,
        1,
    )
    focused_scope = _focused_answer_scope(
        "Give me a case overview.", readiness, focused, evidence
    )
    assert focused_scope["mode"] == "focused_orientation"
    assert "not a reliable matter orientation" in str(focused_scope["notice"])


def test_research_questions_keep_the_original_objective_visible() -> None:
    objective = "Determine the recorded time for event EVT-4821."
    step = research_step_question(objective, "Search logs and later summaries for timing.")
    synthesis = research_synthesis_question(objective)
    assert objective in step
    assert objective in synthesis
    assert "directly" in synthesis.casefold()


def test_objective_synthesis_does_not_require_a_prepared_final_report() -> None:
    source_manifest = {
        "component-event-register": EvidenceItem(
            "S1",
            "Generated event register.csv",
            "Line 7",
            "System event EVT-4821 was accepted at 08:42:17 by reader 4.",
        ),
        "component-dispatch-note": EvidenceItem(
            "S2",
            "Generated dispatch note.txt",
            "Lines 2–3",
            "Dispatch note EVT-4821 references reader 4 and the accepted event.",
        ),
        "prepared-final-summary": EvidenceItem(
            "S3",
            "Generated prepared summary.txt",
            "Line 1",
            "The prepared summary reports EVT-4821 at 08:42:17.",
        ),
    }
    withheld_source_id = "prepared-final-summary"
    supplied_source_ids = (
        "component-event-register",
        "component-dispatch-note",
    )
    component_evidence = tuple(source_manifest[item] for item in supplied_source_ids)

    class ComponentsOnlyGenerator:
        available = True

        def generate(self, **kwargs):
            evidence = tuple(kwargs["evidence"])
            assert source_manifest[withheld_source_id] not in evidence
            supported = next(
                (
                    item
                    for item in evidence
                    if "EVT-4821" in item.excerpt
                    and re.search(r"\b\d{2}:\d{2}:\d{2}\b", item.excerpt)
                ),
                None,
            )
            return {
                "answerable": supported is not None,
                "claims": []
                if supported is None
                else [{"text": supported.excerpt, "evidence_ids": [supported.evidence_id]}],
                "limitation": None,
                "missing_information": "No component record supplied a precise time."
                if supported is None
                else "",
            }

    answer = GroundedGenerationService(ComponentsOnlyGenerator()).answer(
        research_synthesis_question(
            "Determine the exact accepted time for system event EVT-4821."
        ),
        component_evidence,
    )
    assert "08:42:17" in answer.text
    assert answer.used_evidence_ids == ("S1",)
    assert withheld_source_id not in supplied_source_ids


def test_quality_evaluation_pack_is_frozen_and_separable() -> None:
    assert quality_evaluation_fingerprint() == QUALITY_EVALUATION_FINGERPRINT
    categories = {case.category for case in QUALITY_EVALUATION_CASES}
    assert categories == {
        "broad_summary_scope",
        "cross_modal_completeness",
        "generator_objective",
        "retrieval_rank",
    }
    assert any(case.synthesis_source_withheld for case in QUALITY_EVALUATION_CASES)
    assert all("Project" not in case.prompt for case in QUALITY_EVALUATION_CASES)
