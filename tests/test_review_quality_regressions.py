from __future__ import annotations

from dataclasses import replace

import pytest

from case_intelligence.generation import (
    EvidenceItem,
    GenerationGroundingRejected,
    GenerationUnavailable,
    GroundedGenerationService,
)
from case_intelligence.review_bench_v2 import Candidate
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


@pytest.mark.parametrize("header", ["Date", "Sent", "Received", "Created", "Subject"])
def test_exact_record_clocks_ignore_headers_but_keep_body_clocks(header):
    query = "At what time did the amber crate arrive?"
    summary = _exact_record_candidate("summary", "a", "Note.txt", "The amber crate arrived at 09:14.", 0.9)
    email = _exact_record_candidate(
        "email", "b", "Message.eml",
        f"  {header}: 8 October 2036 12:00:00 +0000\nThe amber crate arrived.", 0.4,
    )
    assert rank_exact_record_candidates(query, (summary, email), matter_id="matter-quality") == (summary, email)
    body = replace(email, text=email.text + "\nThe amber crate arrived at 09:14:17.")
    assert rank_exact_record_candidates(query, (summary, body), matter_id="matter-quality") == (body, summary)


def test_exact_record_precision_requires_half_the_subject_terms():
    report = _exact_record_candidate("report", "a", "Report.txt", "Amber crate inspection at the quay.", 0.9)
    decoy = _exact_record_candidate("decoy", "b", "Access log.txt", "Crate accepted at 09:14:17.", 0.4)
    ranked = rank_exact_record_candidates(
        "At what time did the amber crate inspection reach the quay?",
        (report, decoy), matter_id="matter-quality",
    )
    assert ranked == (report, decoy)
    assert ranked[0] is report and ranked[1] is decoy


def test_exact_record_ignores_folded_header_clocks_and_keeps_indented_body():
    query = "At what time did the amber crate arrive?"
    note = _exact_record_candidate("note", "a", "Note.txt", "Amber crate arrival at 09:14.", 0.9)
    email = _exact_record_candidate(
        "email", "b", "Message.eml",
        "Received: synthetic relay;\n  8 October 2036 12:00:00 +0000\n\n  Amber crate arrival.", 0.4,
    )
    assert rank_exact_record_candidates(query, (note, email), matter_id="matter-quality") == (note, email)
    body = replace(email, text=email.text + "\n  Amber crate arrival at 09:14:17.")
    assert rank_exact_record_candidates(query, (note, body), matter_id="matter-quality") == (body, note)
