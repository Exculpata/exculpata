from __future__ import annotations

from dataclasses import replace
import io
from types import SimpleNamespace

import pytest

from case_intelligence.generation import (
    EvidenceItem,
    GenerationGroundingRejected,
    GenerationUnavailable,
    GroundedGenerationService,
)
from case_intelligence.review_bench_v2 import (
    Candidate,
    DeterministicEmbeddingAdapter,
    HybridRetriever,
    InMemoryHybridBackend,
    PostgresHybridBackend,
)
from case_intelligence.review_quality import (
    answer_advances_objective,
    rank_exact_record_candidates,
)


def _candidate(document_id: str, source_name: str, text: str) -> Candidate:
    return Candidate(
        "matter-quality",
        document_id,
        f"chunk-{document_id}",
        source_name,
        1,
        text,
        0.5,
        source_version_id="quality-v1",
        excerpt_digest=(document_id * 64)[:64],
    )


def _exact_record_candidate(
    document: str, chunk: str, name: str, text: str, score: float,
) -> Candidate:
    return replace(
        _candidate(document, name, text),
        chunk_id=chunk,
        score=score,
        excerpt_digest=(chunk * 64)[:64],
    )


def _assert_precise_search_record(record, other):
    pool = (other, record)

    class WeakReranker:
        available = True

        def rerank(self, query, candidates):
            by_id = {item.chunk_id: item for item in candidates}
            return tuple(by_id[item.chunk_id] for item in pool)

    retriever = HybridRetriever(
        InMemoryHybridBackend(pool), DeterministicEmbeddingAdapter(), WeakReranker(),
    )
    query = "At what time was EVT-4821 accepted?"
    for limit in (1, 2):
        ranked = retriever.search("matter-quality", query, limit=limit)
        assert len(ranked) == limit
        for actual, expected in zip(ranked, (record, other)):
            assert replace(actual, score=expected.score) == expected
    assert rank_exact_record_candidates(query, pool, matter_id="matter-quality") == (record, other)


def test_email_body_labeled_clock_survives_limit_one():
    _assert_precise_search_record(
        _candidate("record", "Message.eml", "Timestamp: 08:42:17\nSystem event EVT-4821\nStatus: accepted"),
        _candidate("summary", "Summary.txt", "Summary: EVT-4821 occurred at about 08:40"),
    )


def test_structured_access_log_clock_survives_limit_one():
    _assert_precise_search_record(
        _candidate("record", "Access log.txt", "System event EVT-4821\nTimestamp: 08:42:17\nStatus: accepted"),
        _candidate("summary", "Summary.txt", "Summary: EVT-4821 occurred at about 08:40"),
    )


def test_forwarded_body_with_header_lines_keeps_clock():
    _assert_precise_search_record(
        _candidate("record", "Forwarded.txt", "From: synthetic sender\nSubject: EVT-4821\nTimestamp: 08:42:17\nStatus: accepted"),
        _candidate("summary", "Summary.txt", "Summary: EVT-4821 occurred at about 08:40"),
    )


def test_tagged_email_header_section_adds_no_clock_precision():
    header = replace(
        _candidate("headers", "Message.eml", "Date: 8 Oct 2036 12:00:00 +0000\nSubject: EVT-4821 accepted"),
        location_label="Email headers",
    )
    _assert_precise_search_record(
        _candidate("record", "Message.eml", "EVT-4821 accepted at 08:42."), header,
    )


def test_extracted_email_labels_reach_search_candidates(tmp_path, monkeypatch):
    from case_intelligence.pilot_uploads import PilotStore
    from case_intelligence.review_bench import ReviewBench
    from case_intelligence.workbench import CaseIntelligenceWorkbench

    store = PilotStore(tmp_path / "pilot", malware_scan_mode="disabled")
    document, _ = store.store_stream("Event.eml", "message/rfc822", io.BytesIO(
        b"Date: Wed, 8 Oct 2036 12:00:00 +0000\nSubject: EVT-4821 accepted\n"
        b"Content-Type: text/plain\n\nTimestamp: 08:42\nSystem event EVT-4821\nStatus: accepted\n"
    ))
    assert document.state == "ready"
    reopened = PilotStore(store.root, malware_scan_mode="disabled")
    document = reopened.get(document.document_id)
    units = document.parsed_units()
    bench = ReviewBench.__new__(ReviewBench)
    bench.pilot = reopened
    pilot_candidates = bench._pilot_candidates()
    workbench = CaseIntelligenceWorkbench.__new__(CaseIntelligenceWorkbench)
    matter = SimpleNamespace(matter_id="matter-quality")
    candidates = tuple(workbench._candidate(matter, document, unit, ordinal)
                       for ordinal, unit in enumerate(units, 1))
    assert [item.location_label for item in candidates] == [unit.location_label for unit in units]
    assert [item.location_label for item in pilot_candidates] == [unit.location_label for unit in units]
    assert candidates[0].location_label == "Email headers"
    _assert_precise_search_record(candidates[1], candidates[0])
    store_requests = []

    def source_store(requested_matter):
        store_requests.append(requested_matter)
        return reopened

    monkeypatch.setattr(workbench, "source_store", source_store)
    workbench.learned_retrieval = workbench.postgres_ready = True
    workbench.postgres_connection = object()
    workbench._postgres_lock = None
    workbench.embedding = DeterministicEmbeddingAdapter()
    workbench.reranker = None
    backend = workbench._retriever(matter).backend
    assert store_requests == []
    for candidate in candidates:
        row = (candidate.matter_id, candidate.document_id, candidate.chunk_id,
               candidate.source_name, candidate.page_number, candidate.text, candidate.score,
               candidate.line_start, candidate.line_end, candidate.source_version_id, candidate.excerpt_digest)
        assert backend._candidate(row) == candidate
        for index, value in ((1, "missing-document"), (2, "chunk-99"), (5, "changed text"),
                             (9, "old-version"), (10, "changed-digest")):
            changed = list(row)
            changed[index] = value
            assert backend._candidate(changed).location_label is None
        assert PostgresHybridBackend(None)._candidate(row).location_label is None
        assert PostgresHybridBackend(None, pilot_store=reopened)._candidate(row) == candidate
    assert store_requests == [matter]
    monkeypatch.delattr(workbench, "source_store")
    with pytest.raises(RuntimeError, match="Stored matter identity is invalid"):
        workbench._retriever(matter).backend._candidate(row)


@pytest.mark.parametrize("lane", ["lexical", "dense"])
def test_postgres_label_lookup_releases_connection_lock(lane):
    import threading
    from unittest.mock import MagicMock

    lock = threading.Lock()
    connection = MagicMock()
    row = ("matter-quality", "document", "chunk-1", "Record.txt", 1,
           "Synthetic event", 0.5, None, None, "version-1", "digest")
    connection.cursor.return_value.__enter__.return_value.fetchall.return_value = [row]
    requests = []

    def source_store():
        assert not lock.locked()
        requests.append(True)
        return SimpleNamespace(get=lambda document_id: None)

    backend = PostgresHybridBackend(connection, lock=lock, pilot_store_factory=source_store)
    if lane == "lexical":
        candidates = backend.lexical("matter-quality", "event", 1)
    else:
        candidates = backend.dense("matter-quality", [0.0], "event", 1)
    assert len(candidates) == 1 and candidates[0].text == row[5]
    assert requests == [True]


def test_time_question_keeps_leading_report_ahead_of_email_header_clocks() -> None:
    question = (
        "At what time was the crate inspected and on what date "
        "was the examination planned?"
    )
    report = _candidate(
        "inspection-report",
        "Inspection report.txt",
        "The crate inspection was completed on 6 October 2036. "
        "An examination was planned for the following day.",
    )
    emails = tuple(
        _candidate(
            f"email-{index}",
            f"Filing message {index}.eml",
            "Date: 7 Oct 2036 12:00:00 +0000\n"
            "The crate paperwork is attached for filing.",
        )
        for index in range(12)
    )
    reranked = (report,) + emails
    without_headers = (report,) + tuple(
        replace(item, text=item.text.split("\n", 1)[1]) for item in emails
    )
    control = rank_exact_record_candidates(
        question, without_headers, matter_id="matter-quality"
    )
    assert control[0] is report

    ranked = rank_exact_record_candidates(
        question, reranked, matter_id="matter-quality"
    )
    report_rank = next(
        index for index, item in enumerate(ranked, start=1)
        if item.document_id == report.document_id
    )
    assert report_rank <= 8, (
        f"Email header clocks displaced the reranker's first report to rank {report_rank}"
    )


def test_exact_record_rescue_does_not_match_identifier_prefixes() -> None:
    requested_record = _candidate(
        "dispatch-note",
        "Generated dispatch note.txt",
        "Dispatch note EVT-4821 identifies the requested event.",
    )
    prefix_collision = _candidate(
        "event-log",
        "Access control event log.csv",
        "System event EVT-48210 | accepted | 08:42:17 | reader 4.",
    )

    ranked = rank_exact_record_candidates(
        "At what exact time did system event EVT-4821 occur?",
        (requested_record, prefix_collision),
        matter_id="matter-quality",
    )

    assert [item.document_id for item in ranked] == [
        "dispatch-note",
        "event-log",
    ]


@pytest.mark.parametrize("through_search", [False, True], ids=["ranking", "packet"])
@pytest.mark.parametrize("text", [
    "System event EVT-4821\nTimestamp: 08:42:17\nStatus: accepted",
    "Timestamp: 08:42:17\nSystem event EVT-4821\nStatus: accepted",
    "From: synthetic sender\nDate: 8 Oct 2036 12:00:00 +0000\n\n"
    "System event EVT-4821\nTimestamp: 08:42:17\nStatus: accepted",
])
def test_labeled_event_timestamp_outranks_summary_and_survives_truncation(
    text: str, through_search: bool,
) -> None:
    query = "At what exact time did system event EVT-4821 occur?"
    summary = _candidate(
        "summary", "Later summary.txt", "Event EVT-4821 occurred at about 08:40.",
    )
    record = _candidate("record", "Access log.txt", text)
    pool = (summary, record)

    class WeakReranker:
        available = True

        def rerank(self, query, candidates):
            by_id = {item.document_id: item for item in candidates}
            return tuple(by_id[item.document_id] for item in pool)

    if through_search:
        ranked = HybridRetriever(
            InMemoryHybridBackend(pool), DeterministicEmbeddingAdapter(), WeakReranker(),
        ).search("matter-quality", query, limit=1)
        assert len(ranked) == 1
    else:
        ranked = rank_exact_record_candidates(query, pool, matter_id="matter-quality")
        assert ranked == (record, summary)
        assert ranked[0] is record
    assert ranked[0].document_id == record.document_id
    assert ranked[0].text == record.text
    assert ranked[0].source_version_id == record.source_version_id
    assert ranked[0].excerpt_digest == record.excerpt_digest
    assert ranked[0].chunk_id == record.chunk_id
    assert ranked[0].citation == record.citation


@pytest.mark.parametrize("repair_failure", ["malformed", "unavailable"])
def test_objective_repair_failure_never_returns_known_nonresponsive_draft(
    repair_failure: str,
) -> None:
    evidence = (
        EvidenceItem(
            "S1",
            "Generated event log.csv",
            "Line 7",
            "System event EVT-4821 was accepted at 08:42:17 by reader 4.",
        ),
    )

    class RepairFailureGenerator:
        available = True

        def __init__(self) -> None:
            self.calls = 0

        def generate(self, **kwargs):
            self.calls += 1
            if kwargs["grounding_repair"]:
                if repair_failure == "unavailable":
                    raise GenerationUnavailable("synthetic repair outage")
                return {"unexpected": True}
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

    generator = RepairFailureGenerator()
    with pytest.raises(GenerationGroundingRejected, match="exact objective"):
        GroundedGenerationService(generator).answer(
            "At what exact time did system event EVT-4821 occur?",
            evidence,
        )
    assert generator.calls == 2


def test_broad_summary_counts_distinct_document_ids_with_duplicate_names() -> None:
    evidence = tuple(
        EvidenceItem(
            f"S{index}",
            "duplicate-name.txt",
            f"Line {index}",
            f"Generated record {index} contains distinct substantive fact {index}.",
            document_id=f"document-{index}",
        )
        for index in range(1, 4)
    )

    class BroadGenerator:
        available = True

        def __init__(self) -> None:
            self.calls = 0

        def generate(self, **kwargs):
            self.calls += 1
            selected = (
                kwargs["evidence"]
                if kwargs["grounding_repair"]
                else kwargs["evidence"][:1]
            )
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


def test_broad_summary_does_not_count_passages_from_one_document_as_diverse() -> None:
    evidence = tuple(
        EvidenceItem(
            f"S{index}",
            f"display-name-{index}.txt",
            f"Line {index}",
            f"Generated record contains passage {index}.",
            document_id="one-document",
        )
        for index in range(1, 4)
    )

    class OneSourceGenerator:
        available = True

        def __init__(self) -> None:
            self.calls = 0

        def generate(self, **kwargs):
            self.calls += 1
            item = kwargs["evidence"][0]
            return {
                "answerable": True,
                "claims": [{"text": item.excerpt, "evidence_ids": [item.evidence_id]}],
                "limitation": None,
                "missing_information": "",
            }

    generator = OneSourceGenerator()
    answer = GroundedGenerationService(generator).answer(
        "Give me a case overview.", evidence
    )

    assert generator.calls == 1
    assert answer.used_evidence_ids == ("S1",)


def test_bare_according_to_identifier_is_source_reference() -> None:
    question = "According to DOC-1234, at what exact time did event EVT-4821 occur?"

    assert answer_advances_objective(
        question,
        "Event EVT-4821 occurred at 08:42:17.",
    )
    assert not answer_advances_objective(
        question,
        "Document DOC-1234 was created at 07:15:00; EVT-4821 has no stated time.",
    )
    dual_role_question = (
        "According to DOC-1234, report the exact time DOC-1234 was created "
        "and event EVT-4821 occurred."
    )
    assert not answer_advances_objective(
        dual_role_question,
        "Event EVT-4821 occurred at 08:42:17.",
    )
    assert answer_advances_objective(
        dual_role_question,
        "DOC-1234 was created at 07:15:00; EVT-4821 occurred at 08:42:17.",
    )


def test_exact_record_precision_requires_half_the_subject_terms():
    report = _exact_record_candidate("report", "a", "Report.txt", "Amber crate inspection at the quay.", 0.9)
    decoy = _exact_record_candidate("decoy", "b", "Access log.txt", "Crate accepted at 09:14:17.", 0.4)
    ranked = rank_exact_record_candidates(
        "At what time did the amber crate inspection reach the quay?",
        (report, decoy), matter_id="matter-quality",
    )
    assert ranked == (report, decoy)
    assert ranked[0] is report and ranked[1] is decoy


