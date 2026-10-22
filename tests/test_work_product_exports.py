from __future__ import annotations


from case_intelligence.legacy_names import (
    LEGACY_PRODUCT,
    LEGACY_STEM,
)
import csv
import io
import json
import re
import zipfile
from dataclasses import replace
from xml.etree import ElementTree

import pytest

from case_intelligence.media_evidence import (
    export_transcript,
    transcript_summary_windows,
    transcript_units,
)
from case_intelligence.work_product_exports import (
    DOCX_MEDIA_TYPE,
    ExportProblem,
    export_answer,
    export_conversation,
    export_full_review,
    export_matter_bundle,
    export_report,
    export_research,
)
from case_intelligence.workspace_store import (
    ConversationRecord,
    MatterRecord,
    MessageRecord,
    NotebookItemRecord,
    NotebookReferenceRecord,
    ResearchJobRecord,
    ReportCitationRecord,
    ReportRecord,
    ReportSectionRecord,
    ReviewCriterionRecord,
    ReviewCriterionVersionRecord,
    ReviewDecisionRecord,
    ReviewRunRecord,
    TranscriptSegmentRecord,
)


STAMP = "2026-08-27T15:00:00Z"
INTERNAL_DIGEST = "a9" * 32
INTERNAL_REVIEWER = "principal-reviewer-internal"
INTERNAL_LINK = "/matters/internal-only?support=opaque-token"
INTERNAL_CLUSTER = "SPEAKER_00"

FORBIDDEN_PORTABLE_KEYS = {
    "id",
    "matter_id",
    "criterion_id",
    "criterion_version_id",
    "run_id",
    "document_id",
    "source_version_id",
    "reference_id",
    "item_id",
    "segment_id",
    "transcript_id",
    "action_token",
    "support_token",
    "href",
    "excerpt_digest",
    "chunk_id",
    "unit_number",
    "line_start",
    "line_end",
    "reviewed_by",
    "confidence",
    "model_score",
    "machine_text",
    "text_revision",
    "speaker_revision",
    "speaker_cluster",
    "low_confidence",
    "overlap",
}


def _walk_json(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_json(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_json(child)


def _assert_portable_json(value) -> None:
    for item in _walk_json(value):
        assert FORBIDDEN_PORTABLE_KEYS.isdisjoint(item)
    rendered = json.dumps(value, ensure_ascii=False)
    assert re.search(r"(?<![0-9a-f])[0-9a-f]{64}(?![0-9a-f])", rendered, re.I) is None
    for marker in (
        INTERNAL_REVIEWER,
        INTERNAL_LINK,
        INTERNAL_CLUSTER,
        "machine_draft",
        "human_reviewed",
        "cluster",
    ):
        assert marker not in rendered


def _review_records(matter: MatterRecord):
    criterion = ReviewCriterionRecord(
        criterion_id="criterion-" + "1" * 32,
        matter_id=matter.matter_id,
        title="Generated event check",
        created_by="principal-criterion-author",
        created_at=STAMP,
        updated_by="principal-criterion-author",
        updated_at=STAMP,
        version_count=1,
        current_version_number=1,
        current_version_id="criterion-version-" + "2" * 32,
    )
    version = ReviewCriterionVersionRecord(
        criterion_version_id=criterion.current_version_id,
        criterion_id=criterion.criterion_id,
        matter_id=matter.matter_id,
        version_number=1,
        instructions="Identify sources that record the generated event.",
        include_guidance="Include an exact contemporaneous record.",
        exclude_guidance="Exclude unrelated summaries.",
        created_by="principal-criterion-author",
        created_at=STAMP,
    )
    run = ReviewRunRecord(
        run_id="review-run-" + "3" * 32,
        matter_id=matter.matter_id,
        actor_id="principal-review-owner",
        criterion_id=criterion.criterion_id,
        criterion_version_id=version.criterion_version_id,
        source_set_id=None,
        run_kind="full",
        state="succeeded",
        stage="complete",
        attempts=1,
        worker_id="internal-worker",
        cancellation_requested=0,
        snapshot_count=1,
        reviewed_count=1,
        included_count=1,
        excluded_count=0,
        attention_count=0,
        message="Complete",
        created_at=STAMP,
        started_at=STAMP,
        last_claimed_at=STAMP,
        finished_at=STAMP,
        updated_at=STAMP,
    )
    decision = ReviewDecisionRecord(
        run_id=run.run_id,
        matter_id=matter.matter_id,
        ordinal=1,
        document_id="document-" + "4" * 32,
        source_version_id="source-version-" + "5" * 32,
        source_basis_digest=INTERNAL_DIGEST,
        action_token="synthetic-internal-action-token",
        source_name="Generated register.pdf",
        source_kind="PDF",
        machine_decision="included",
        rationale="The contemporaneous entry meets the saved criterion.",
        citations=(
            {
                "source_name": "Generated register.pdf",
                "location": "Page 4",
                "excerpt": "The generated event was entered at 21:14.",
                "evidence_kind": "document",
                "href": INTERNAL_LINK,
                "support_token": "6" * 40,
                "matter_id": matter.matter_id,
                "document_id": "document-" + "4" * 32,
                "source_version_id": "source-version-" + "5" * 32,
                "excerpt_digest": INTERNAL_DIGEST,
                "chunk_id": "internal-chunk",
                "unit_number": 4,
                "line_start": 7,
                "line_end": 9,
            },
        ),
        error_message="",
        validation_sample=1,
        human_decision="agree",
        human_note="Checked against the cited page.",
        reviewed_by=INTERNAL_REVIEWER,
        reviewed_at=STAMP,
        created_at=STAMP,
        updated_at=STAMP,
    )
    metrics = {
        "sample_total": 1,
        "reviewed_total": 1,
        "adjudicated_total": 1,
        "uncertain_total": 0,
        "unscored_total": 0,
        "true_positive": 1,
        "false_positive": 0,
        "false_negative": 0,
        "true_negative": 0,
        "precision": 1.0,
        "recall": 1.0,
        "elusion": None,
        "richness": 1.0,
        "error_rate": 0.0,
        "model_score": 0.987654,
        "document_id": decision.document_id,
        "digest": INTERNAL_DIGEST,
    }
    return criterion, version, run, decision, metrics


def _transcript_segments(matter: MatterRecord):
    common = {
        "transcript_id": "transcript-" + "6" * 32,
        "matter_id": matter.matter_id,
        "translated_text": None,
        "created_at": STAMP,
    }
    return (
        TranscriptSegmentRecord(
            segment_id="segment-" + "7" * 32,
            ordinal=1,
            external_segment_id="processor-segment-1",
            start_ms=1_250,
            end_ms=2_345,
            speaker_cluster=INTERNAL_CLUSTER,
            model_text="Internal original wording that must not be portable.",
            confidence=0.987654,
            low_confidence=0,
            overlap=1,
            current_text="A staff-confirmed speaker described the generated entry.",
            current_revision=2,
            speaker_display_name="Speaker Smith",
            speaker_identity_state="confirmed",
            speaker_revision=1,
            **common,
        ),
        TranscriptSegmentRecord(
            segment_id="segment-" + "8" * 32,
            ordinal=2,
            external_segment_id="processor-segment-2",
            start_ms=2_345,
            end_ms=4_500,
            speaker_cluster="SPEAKER_01",
            model_text="Second internal original wording.",
            confidence=0.4321,
            low_confidence=1,
            overlap=0,
            current_text="An unconfirmed speaker supplied a second generated detail.",
            current_revision=0,
            speaker_display_name="SPEAKER_01",
            speaker_identity_state="cluster",
            speaker_revision=0,
            **common,
        ),
    )


def _records():
    matter = MatterRecord(
        "ci-matter-" + "a" * 32,
        "m-" + "a" * 12,
        "Synthetic & <Matter>",
        "Generated fixture",
        "principal-owner",
        STAMP,
        STAMP,
    )
    conversation = ConversationRecord(
        "conversation-" + "b" * 32,
        matter.matter_id,
        "Loading dock timeline",
        STAMP,
        STAMP,
    )
    question = MessageRecord(
        "message-" + "c" * 32,
        conversation.conversation_id,
        1,
        "user",
        "What happened near the loading dock?",
        {},
        STAMP,
    )
    answer = MessageRecord(
        "message-" + "d" * 32,
        conversation.conversation_id,
        2,
        "assistant",
        "A supported answer.",
        {
            "kind": "generated",
            "introduction": "The supplied record supports one event.",
            "claims": [
                {
                    "text": "A vehicle arrived at 10:12 p.m.",
                    "citations": [
                        {
                            "source_name": "Generated report.pdf",
                            "location": "Page 2",
                            "href": "/internal/not-portable",
                        }
                    ],
                }
            ],
            "limitation": {
                "text": "The record does not identify the driver.",
                "citations": [],
            },
            "source_coverage": {
                "mode": "partial",
                "searchable_count": 10,
                "total_count": 11,
                "excluded_count": 1,
                "notice": (
                    "Search and answers use 10 of 11 sources. "
                    "1 source needs attention and is excluded."
                ),
            },
            "review_scope": {
                "mode": "focused",
                "collection_wide_request": True,
                "searchable_source_count": 10,
                "candidate_passage_count": 12,
                "candidate_source_count": 8,
                "cited_passage_count": 1,
                "cited_source_count": 1,
                "notice": (
                    "This is a focused answer from the highest-ranked passages, "
                    "not a document-by-document completeness review."
                ),
            },
            "modality_coverage": {
                "mode": "partial",
                "requested_evidence_kinds": ["written", "spoken"],
                "available_evidence_kinds": ["written"],
                "used_evidence_kinds": ["written"],
                "missing_evidence_kinds": ["spoken"],
                "notice": "This result is partial because spoken support was not retained.",
            },
        },
        STAMP,
    )
    return matter, conversation, question, answer


def test_answer_and_conversation_exports_are_readable_markdown_and_docx():
    matter, conversation, question, answer = _records()
    markdown = export_answer(
        matter,
        conversation,
        answer,
        question,
        "markdown",
        exported_at=STAMP,
    )
    assert markdown.media_type.startswith("text/markdown")
    text = markdown.body.decode()
    assert "Synthetic & \\<Matter\\>" in text
    assert "A vehicle arrived at 10:12 p.m." in text
    assert "Generated report.pdf — Page 2" in text
    assert "Search and answers use 10 of 11 sources" in text
    assert "Completeness caution" in text
    assert "not a document-by-document completeness review" in text
    assert "/internal/not-portable" not in text

    docx = export_conversation(
        matter,
        conversation,
        (question, answer),
        "docx",
        exported_at=STAMP,
    )
    assert docx.media_type == DOCX_MEDIA_TYPE
    with zipfile.ZipFile(io.BytesIO(docx.body)) as archive:
        assert archive.testzip() is None
        document = archive.read("word/document.xml")
        ElementTree.fromstring(document)
        rendered = document.decode()
        assert "Loading dock timeline" in rendered
        assert "Generated report.pdf — Page 2" in rendered
        assert "&lt;Matter&gt;" in rendered

    other_matter = replace(matter, matter_id="ci-matter-" + "e" * 32)
    with pytest.raises(ExportProblem, match="assistant answer"):
        export_answer(
            other_matter,
            conversation,
            answer,
            question,
            "markdown",
            exported_at=STAMP,
        )
    research = ResearchJobRecord(
        job_id="research-job-" + "d" * 32,
        matter_id=matter.matter_id,
        actor_id="principal-researcher",
        idempotency_key="synthetic-research-request",
        question="What does the generated record show?",
        title="Generated investigation",
        source_set_id=None,
        conversation_id=conversation.conversation_id,
        result_message_id=None,
        state="succeeded",
        stage="complete",
        attempts=1,
        worker_id=None,
        cancellation_requested=0,
        plan={},
        result={},
        total_steps=1,
        completed_steps=1,
        candidate_count=0,
        evidence_count=0,
        message="Complete",
        created_at=STAMP,
        started_at=STAMP,
        last_claimed_at=STAMP,
        finished_at=STAMP,
        updated_at=STAMP,
    )
    with pytest.raises(ExportProblem, match="matter boundary"):
        export_research(
            other_matter, research, "json", exported_at=STAMP
        )


def test_matter_bundle_has_portable_work_product_without_originals_or_internal_links():
    matter, conversation, question, answer = _records()
    artifact = export_matter_bundle(
        matter,
        ((conversation, (question, answer)),),
        (
            {
                "name": "Generated report.pdf",
                "kind": "PDF",
                "state": "Searchable",
                "count_label": "2 pages",
            },
        ),
        exported_at=STAMP,
    )
    with zipfile.ZipFile(io.BytesIO(artifact.body)) as archive:
        names = set(archive.namelist())
        assert {
            "README.txt",
            "manifest.json",
            "conversations.json",
            "matter-report.md",
            "matter-report.docx",
        }.issubset(names)
        assert any(name.startswith("conversations/001-Loading-dock-timeline") for name in names)
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["original_source_files_included"] is False
        assert manifest["source_count"] == 1
        portable = archive.read("conversations.json").decode()
        assert "A vehicle arrived at 10:12 p.m." in portable
        assert '"excluded_count": 1' in portable
        assert '"collection_wide_request": true' in portable
        assert '"candidate_passage_count": 12' in portable
        assert '"requested": [' in portable
        assert '"missing": [' in portable
        assert "spoken support was not retained" in portable
        assert "Search and answers use 10 of 11 sources" in portable
        assert "/internal/not-portable" not in portable
        assert matter.matter_id not in portable
        assert conversation.conversation_id not in portable
        assert "Original source files are not included" in archive.read(
            "matter-report.md"
        ).decode()


def test_every_source_csv_and_json_keep_exact_support_without_internal_metadata():
    matter, _conversation, _question, _answer = _records()
    criterion, version, run, decision, metrics = _review_records(matter)

    json_artifact = export_full_review(
        matter,
        criterion,
        version,
        run,
        (decision,),
        metrics,
        "json",
        exported_at=STAMP,
    )
    assert json_artifact.filename.endswith("-source-check.json")
    assert "full-review" not in json_artifact.filename
    payload = json.loads(json_artifact.body)
    _assert_portable_json(payload)
    assert payload["schema"] == f"{LEGACY_STEM}-source-check-v1"
    assert payload["matter"] == {"name": matter.display_name}
    assert payload["check"]["scope"] == "Every source"
    assert payload["check"]["status"] == "Succeeded"
    assert payload["decisions"] == [
        {
            "ordinal": 1,
            "source_name": "Generated register.pdf",
            "source_kind": "PDF",
            f"{LEGACY_STEM}_label": "Included",
            "rationale": "The contemporaneous entry meets the saved criterion.",
            "citations": [
                {
                    "source_name": "Generated register.pdf",
                    "location": "Page 4",
                    "excerpt": "The generated event was entered at 21:14.",
                }
            ],
            "attention_note": "",
            "validation_sample": True,
            "staff_decision": "Agree",
            "staff_note": "Checked against the cited page.",
            "reviewed_at": STAMP,
        }
    ]

    csv_artifact = export_full_review(
        matter,
        criterion,
        version,
        run,
        (decision,),
        metrics,
        "csv",
        exported_at=STAMP,
    )
    csv_text = csv_artifact.body.decode("utf-8-sig")
    rows = list(csv.DictReader(io.StringIO(csv_text)))
    assert tuple(rows[0]) == (
        "Ordinal",
        "Source",
        "Type",
        f"{LEGACY_PRODUCT} label",
        "Rationale",
        "Cited support",
        "Needs attention",
        "Validation sample",
        "Staff decision",
        "Staff note",
        "Reviewed at",
    )
    assert rows[0]["Cited support"] == (
        "Generated register.pdf — Page 4 — "
        "The generated event was entered at 21:14."
    )
    assert rows[0][f"{LEGACY_PRODUCT} label"] == "Included"
    assert rows[0]["Staff decision"] == "Agree"
    assert rows[0]["Reviewed at"] == STAMP
    for marker in (
        INTERNAL_REVIEWER,
        INTERNAL_LINK,
        INTERNAL_DIGEST,
        decision.document_id,
        decision.source_version_id,
    ):
        assert marker not in csv_text

    other_matter = replace(matter, matter_id="ci-matter-" + "f" * 32)
    with pytest.raises(ExportProblem, match="matter boundary"):
        export_full_review(
            other_matter,
            criterion,
            version,
            run,
            (decision,),
            metrics,
            "json",
            exported_at=STAMP,
        )


@pytest.mark.parametrize("format_name", ("csv", "json", "markdown", "docx"))
def test_every_source_exports_refuse_unbound_or_unsupported_inclusions(format_name):
    matter, _conversation, _question, _answer = _records()
    criterion, version, run, decision, metrics = _review_records(matter)
    foreign_marker = "Generated foreign matter marker"
    malformed = dict(decision.citations[0])
    malformed["matter_id"] = "ci-matter-" + "f" * 32
    malformed["excerpt"] = foreign_marker

    with pytest.raises(ExportProblem, match="did not match its source"):
        export_full_review(
            matter,
            criterion,
            version,
            run,
            (replace(decision, citations=(malformed,)),),
            metrics,
            format_name,
            exported_at=STAMP,
        )
    with pytest.raises(ExportProblem, match="no exact source support"):
        export_full_review(
            matter,
            criterion,
            version,
            run,
            (replace(decision, citations=()),),
            metrics,
            format_name,
            exported_at=STAMP,
        )
    malformed_citation = dict(decision.citations[0])
    malformed_citation["location"] = ""
    with pytest.raises(ExportProblem, match="exact location"):
        export_full_review(
            matter,
            criterion,
            version,
            run,
            (replace(decision, citations=(malformed_citation,)),),
            metrics,
            "json",
            exported_at=STAMP,
        )


def test_transcript_csv_and_json_use_staff_speaker_labels_without_processor_fields():
    matter, _conversation, _question, _answer = _records()
    segments = _transcript_segments(matter)

    json_export = export_transcript("Generated interview.wav", segments, "json")
    payload = json.loads(json_export.body)
    _assert_portable_json(payload)
    assert payload == {
        "source_name": "Generated interview.wav",
        "source_kind": "Transcript",
        "review_status": "Staff edits saved",
        "segments": [
            {
                "ordinal": 1,
                "start": "00:00:01.250",
                "end": "00:00:02.345",
                "speaker": "Speaker Smith",
                "speaker_status": "Confirmed",
                "text": "A staff-confirmed speaker described the generated entry.",
            },
            {
                "ordinal": 2,
                "start": "00:00:02.345",
                "end": "00:00:04.500",
                "speaker": "Speaker 1",
                "speaker_status": "Unconfirmed",
                "text": "An unconfirmed speaker supplied a second generated detail.",
            },
        ],
    }

    csv_export = export_transcript("Generated interview.wav", segments, "csv")
    csv_text = csv_export.body.decode("utf-8-sig")
    rows = list(csv.DictReader(io.StringIO(csv_text)))
    assert tuple(rows[0]) == (
        "Source",
        "Type",
        "Ordinal",
        "Start",
        "End",
        "Speaker",
        "Speaker status",
        "Text",
    )
    assert rows[0]["Source"] == "Generated interview.wav"
    assert rows[0]["Type"] == "Transcript"
    assert rows[0]["Speaker"] == "Speaker Smith"
    assert rows[0]["Start"] == "00:00:01.250"
    assert rows[1]["Speaker"] == "Speaker 1"
    assert rows[1]["Speaker status"] == "Unconfirmed"
    for marker in (
        INTERNAL_CLUSTER,
        "SPEAKER_01",
        "Internal original wording",
        "0.987654",
        segments[0].segment_id,
        segments[0].transcript_id,
        matter.matter_id,
    ):
        assert marker not in csv_text

    expected_times = {
        "txt": "00:00:01.250–00:00:02.345",
        "markdown": "00:00:01.250–00:00:02.345",
        "srt": "00:00:01,250 --> 00:00:02,345",
        "vtt": "00:00:01.250 --> 00:00:02.345",
    }
    for format_name, timestamp in expected_times.items():
        readable = export_transcript(
            "Generated interview.wav", segments, format_name
        ).body.decode()
        assert timestamp in readable
        assert "Speaker Smith" in readable
        assert "Speaker 1" in readable
        assert "A staff-confirmed speaker described the generated entry." in readable
        assert INTERNAL_CLUSTER not in readable
        assert "SPEAKER_01" not in readable
        assert "Internal original wording" not in readable

    docx = export_transcript("Generated interview.wav", segments, "docx")
    with zipfile.ZipFile(io.BytesIO(docx.body)) as archive:
        document_xml = archive.read("word/document.xml").decode()
    assert "00:00:01.250–00:00:02.345" in document_xml
    assert "Speaker Smith" in document_xml
    assert "Speaker 1" in document_xml
    assert "A staff-confirmed speaker described the generated entry." in document_xml
    assert INTERNAL_CLUSTER not in document_xml
    assert "SPEAKER_01" not in document_xml

    with pytest.raises(ValueError, match="mixed sources"):
        export_transcript(
            "Generated interview.wav",
            (segments[0], replace(segments[1], matter_id="ci-matter-" + "e" * 32)),
            "json",
        )


def test_anonymous_speaker_labels_match_projection_every_export_and_bundle():
    matter, _conversation, _question, _answer = _records()
    original = _transcript_segments(matter)
    segments = (
        replace(
            original[0],
            speaker_cluster="SPEAKER_01",
            speaker_display_name="SPEAKER_01",
            speaker_identity_state="cluster",
            speaker_revision=0,
            current_text="The first generated anonymous passage.",
        ),
        replace(
            original[1],
            speaker_cluster="SPEAKER_00",
            speaker_display_name="SPEAKER_00",
            current_text="The second generated anonymous passage.",
        ),
    )

    projected = transcript_units(segments)
    assert [item.text for item in projected] == [
        "Speaker 2: The first generated anonymous passage.",
        "Speaker 1: The second generated anonymous passage.",
    ]
    summary_excerpt = transcript_summary_windows(segments)[0].excerpt
    assert "Speaker 2: The first generated anonymous passage." in summary_excerpt
    assert "Speaker 1: The second generated anonymous passage." in summary_excerpt
    assert "SPEAKER_00" not in summary_excerpt
    assert "SPEAKER_01" not in summary_excerpt

    exports = {
        format_name: export_transcript(
            "Generated anonymous interview.wav", segments, format_name
        )
        for format_name in ("txt", "markdown", "docx", "srt", "vtt", "csv", "json")
    }
    payload = json.loads(exports["json"].body)
    assert [
        (item["ordinal"], item["speaker"], item["text"])
        for item in payload["segments"]
    ] == [
        (1, "Speaker 2", "The first generated anonymous passage."),
        (2, "Speaker 1", "The second generated anonymous passage."),
    ]
    rows = list(
        csv.DictReader(io.StringIO(exports["csv"].body.decode("utf-8-sig")))
    )
    assert [
        (int(item["Ordinal"]), item["Speaker"], item["Text"])
        for item in rows
    ] == [
        (1, "Speaker 2", "The first generated anonymous passage."),
        (2, "Speaker 1", "The second generated anonymous passage."),
    ]

    for format_name in ("txt", "srt", "vtt"):
        rendered = exports[format_name].body.decode()
        assert "Speaker 2: The first generated anonymous passage." in rendered
        assert "Speaker 1: The second generated anonymous passage." in rendered
    markdown = exports["markdown"].body.decode()
    assert (
        "Speaker 2 (Unconfirmed)**\n\nThe first generated anonymous passage."
        in markdown
    )
    assert (
        "Speaker 1 (Unconfirmed)**\n\nThe second generated anonymous passage."
        in markdown
    )
    with zipfile.ZipFile(io.BytesIO(exports["docx"].body)) as archive:
        document_xml = archive.read("word/document.xml").decode()
    assert document_xml.index("Speaker 2") < document_xml.index(
        "The first generated anonymous passage."
    )
    assert document_xml.index("Speaker 1") < document_xml.index(
        "The second generated anonymous passage."
    )

    bundle = export_matter_bundle(
        matter,
        (),
        (),
        media_work_product=(
            {
                "matter_id": matter.matter_id,
                "source_name": "Generated anonymous interview.wav",
                "source_kind": "Audio",
                "review_state": "machine_draft",
                "segment_count": 2,
                "markdown": exports["markdown"].body,
                "srt": exports["srt"].body,
                "json": exports["json"].body,
                "summary_markdown": None,
                "summary_coverage": None,
                "clips": (),
            },
        ),
        exported_at=STAMP,
    )
    with zipfile.ZipFile(io.BytesIO(bundle.body)) as archive:
        bundled = {
            suffix: archive.read(
                next(
                    name
                    for name in archive.namelist()
                    if name.startswith("transcripts/") and name.endswith(suffix)
                )
            )
            for suffix in (".md", ".srt", ".json")
        }
    assert bundled == {
        ".md": exports["markdown"].body,
        ".srt": exports["srt"].body,
        ".json": exports["json"].body,
    }
    for artifact in (*exports.values(), bundle):
        assert b"SPEAKER_00" not in artifact.body
        assert b"SPEAKER_01" not in artifact.body


def test_confirmed_raw_looking_speaker_name_is_preserved_in_every_transcript_export():
    matter, _conversation, _question, _answer = _records()
    original = _transcript_segments(matter)
    segments = (replace(original[0], speaker_display_name="Speaker 7"), original[1])

    for format_name in ("txt", "markdown", "srt", "vtt", "csv", "json"):
        rendered = export_transcript(
            "Generated interview.wav", segments, format_name
        ).body.decode("utf-8-sig")
        assert "Speaker 7" in rendered
        assert "SPEAKER_00" not in rendered
    docx = export_transcript("Generated interview.wav", segments, "docx")
    with zipfile.ZipFile(io.BytesIO(docx.body)) as archive:
        rendered_docx = archive.read("word/document.xml").decode()
    assert "Speaker 7" in rendered_docx
    assert "SPEAKER_00" not in rendered_docx

    transcript_json = export_transcript(
        "Generated interview.wav", segments, "json"
    )
    transcript_markdown = export_transcript(
        "Generated interview.wav", segments, "markdown"
    )
    transcript_srt = export_transcript(
        "Generated interview.wav", segments, "srt"
    )
    bundle = export_matter_bundle(
        matter,
        (),
        (),
        media_work_product=(
            {
                "matter_id": matter.matter_id,
                "source_name": "Generated interview.wav",
                "source_kind": "Audio",
                "review_state": "human_reviewed",
                "segment_count": 2,
                "markdown": transcript_markdown.body,
                "srt": transcript_srt.body,
                "json": transcript_json.body,
                "summary_markdown": None,
                "summary_coverage": None,
                "clips": (),
            },
        ),
        exported_at=STAMP,
    )
    with zipfile.ZipFile(io.BytesIO(bundle.body)) as archive:
        transcript_name = next(
            name
            for name in archive.namelist()
            if name.startswith("transcripts/") and name.endswith(".json")
        )
        assert "Speaker 7" in archive.read(transcript_name).decode()


@pytest.mark.parametrize("format_name", ("markdown", "docx"))
def test_report_exports_refuse_cross_matter_citations(format_name):
    matter, _conversation, _question, _answer = _records()
    report = ReportRecord(
        report_id="report-" + "1" * 32,
        matter_id=matter.matter_id,
        title="Generated chronology",
        purpose="Synthetic report export regression",
        status="draft",
        created_by="principal-author",
        created_at=STAMP,
        updated_by="principal-author",
        updated_at=STAMP,
        section_count=1,
    )
    section = ReportSectionRecord(
        section_id="report-section-" + "2" * 32,
        report_id=report.report_id,
        matter_id=matter.matter_id,
        ordinal=1,
        heading="Generated event",
        body="A generated record supports this section.",
        origin="staff",
        origin_id="",
        created_by="principal-author",
        created_at=STAMP,
        updated_by="principal-author",
        updated_at=STAMP,
    )
    citation = ReportCitationRecord(
        citation_id="report-citation-" + "3" * 32,
        section_id=section.section_id,
        report_id=report.report_id,
        matter_id=matter.matter_id,
        ordinal=1,
        kind="source",
        document_id="4" * 32,
        source_version_id="5" * 32,
        source_name="Generated record.pdf",
        location="Page 3",
        support_token="6" * 40,
        excerpt="Generated exact support.",
        media_clip_id="",
        start_ms=0,
        end_ms=0,
        created_at=STAMP,
    )
    valid = export_report(
        matter,
        report,
        ((section, (citation,)),),
        format_name,
        exported_at=STAMP,
    )
    assert valid.body

    foreign_marker = "Generated foreign report marker"
    with pytest.raises(ExportProblem, match="matter boundary"):
        export_report(
            matter,
            report,
            (
                (
                    section,
                    (
                        replace(
                            citation,
                            matter_id="ci-matter-" + "f" * 32,
                            excerpt=foreign_marker,
                        ),
                    ),
                ),
            ),
            format_name,
            exported_at=STAMP,
        )


def test_bundle_json_csv_and_names_are_portable_across_saved_work_product():
    matter, conversation, question, answer = _records()
    criterion, version, run, decision, metrics = _review_records(matter)
    source_json = export_full_review(
        matter,
        criterion,
        version,
        run,
        (decision,),
        metrics,
        "json",
        exported_at=STAMP,
    )
    source_csv = export_full_review(
        matter,
        criterion,
        version,
        run,
        (decision,),
        metrics,
        "csv",
        exported_at=STAMP,
    )
    notebook_item = NotebookItemRecord(
        item_id="notebook-item-" + "9" * 32,
        matter_id=matter.matter_id,
        item_type="fact",
        status="confirmed",
        title="Generated event note",
        body="The entry is supported by the cited generated page.",
        date_label="2026-08-27",
        is_pinned=1,
        origin="staff",
        source_conversation_id=conversation.conversation_id,
        source_message_id=answer.message_id,
        created_by="principal-notebook-author",
        created_at=STAMP,
        updated_by="principal-notebook-reviewer",
        updated_at=STAMP,
        created_by_name="",
        updated_by_name="",
        reference_count=1,
    )
    notebook_reference = NotebookReferenceRecord(
        reference_id="notebook-reference-" + "b" * 32,
        item_id=notebook_item.item_id,
        matter_id=matter.matter_id,
        ordinal=1,
        document_id=decision.document_id,
        source_version_id=decision.source_version_id,
        source_name="Generated register.pdf",
        location="Page 4",
        unit_number=4,
        chunk_id="internal-notebook-chunk",
        excerpt_digest=INTERNAL_DIGEST,
        excerpt="The generated event was entered at 21:14.",
        support_token="synthetic-internal-notebook-support",
        created_at=STAMP,
    )
    segments = _transcript_segments(matter)
    transcript_json = export_transcript("Generated interview.wav", segments, "json")
    transcript_markdown = export_transcript(
        "Generated interview.wav", segments, "markdown"
    )
    transcript_srt = export_transcript("Generated interview.wav", segments, "srt")

    artifact = export_matter_bundle(
        matter,
        ((conversation, (question, answer)),),
        (
            {
                "name": "Generated register.pdf",
                "kind": "PDF",
                "state": "Searchable",
                "count_label": "4 pages",
            },
            {
                "name": "Generated interview.wav",
                "kind": "Audio",
                "state": "Searchable",
                "count_label": "1 transcript",
            },
        ),
        ((notebook_item, (notebook_reference,)),),
        (
            {
                "matter_id": matter.matter_id,
                "source_name": "Generated interview.wav",
                "source_kind": "Audio",
                "review_state": "human_reviewed",
                "segment_count": 2,
                "markdown": transcript_markdown.body,
                "srt": transcript_srt.body,
                "json": transcript_json.body,
                "summary_markdown": None,
                "summary_coverage": {
                    "mode": "representative",
                    "covered_segment_count": 2,
                    "total_segment_count": 3,
                    "confidence": 0.7654,
                    "model_id": "internal-model",
                    "basis_digest": INTERNAL_DIGEST,
                },
                "clips": (),
            },
        ),
        (
            {
                "kind": "investigation",
                "path": "investigations/001-generated-investigation.json",
                "body": b'{"title":"Generated investigation"}',
            },
            {
                "kind": "source_check",
                "path": f"source-checks/001-{source_json.filename}",
                "body": source_json.body,
            },
            {
                "kind": "source_check",
                "path": f"source-checks/001-{source_csv.filename}",
                "body": source_csv.body,
            },
        ),
        exported_at=STAMP,
    )

    with zipfile.ZipFile(io.BytesIO(artifact.body)) as archive:
        names = archive.namelist()
        lowered_names = "\n".join(names).casefold()
        assert "case-intelligence" not in lowered_names
        assert "investigations/001-generated-investigation.json" in names
        assert any(name.startswith("source-checks/") for name in names)
        assert "full-review" not in lowered_names
        assert not any(name.startswith("research/") for name in names)

        manifest = json.loads(archive.read("manifest.json"))
        _assert_portable_json(manifest)
        additional = manifest["additional_work_product"]
        assert {item["kind"] for item in additional} == {
            "investigation",
            "source_check",
        }
        assert all(
            item["path"].startswith(("investigations/", "source-checks/"))
            for item in additional
        )
        manifest_text = json.dumps(manifest)
        assert "deep_research" not in manifest_text
        assert "full_review" not in manifest_text

        notebook = json.loads(archive.read("notebook/notebook.json"))
        _assert_portable_json(notebook)
        assert notebook["items"][0]["sources"] == [
            {
                "name": "Generated register.pdf",
                "location": "Page 4",
                "excerpt": "The generated event was entered at 21:14.",
            }
        ]
        assert notebook["items"][0]["type"] == "Fact"
        assert notebook["items"][0]["status"] == "Confirmed"
        assert notebook["items"][0]["origin"] == "Staff"
        assert notebook["items"][0]["created_by"] == "Staff member"
        assert notebook["items"][0]["updated_by"] == "Staff member"

        media = json.loads(archive.read("media/media-work-product.json"))
        _assert_portable_json(media)
        assert media["transcripts"][0]["source_kind"] == "Audio"
        assert media["transcripts"][0]["review_status"] == "Staff edits saved"
        assert media["transcripts"][0]["overview_coverage"] == {
            "scope": "Representative passages",
            "covered_passages": 2,
            "total_passages": 3,
        }

        for name in names:
            member = archive.read(name)
            assert b"case-intelligence" not in member.lower(), name
            if name.endswith(".docx"):
                with zipfile.ZipFile(io.BytesIO(member)) as nested:
                    for nested_name in nested.namelist():
                        assert b"case-intelligence" not in nested.read(
                            nested_name
                        ).lower(), f"{name}:{nested_name}"
            if not name.endswith((".json", ".csv", ".md", ".srt", ".txt")):
                continue
            body = member.decode("utf-8-sig")
            assert INTERNAL_DIGEST not in body, name
            assert INTERNAL_LINK not in body, name
            assert INTERNAL_REVIEWER not in body, name
            assert "principal-notebook-author" not in body, name
            assert "principal-notebook-reviewer" not in body, name
            assert INTERNAL_CLUSTER not in body, name
            assert "SPEAKER_01" not in body, name
            assert "machine_draft" not in body, name
            assert "human_reviewed" not in body, name
            assert "deep_research" not in body, name
            assert "full_review" not in body, name
            assert re.search(
                r"(?<![0-9a-f])[0-9a-f]{64}(?![0-9a-f])", body, re.I
            ) is None, name

    with pytest.raises(ExportProblem, match="matter boundary"):
        export_matter_bundle(
            matter,
            (),
            (),
            media_work_product=(
                {
                    "matter_id": "ci-matter-" + "c" * 32,
                    "source_name": "Cross-matter recording.wav",
                    "source_kind": "Audio",
                    "review_state": "machine_draft",
                    "segment_count": 2,
                    "markdown": transcript_markdown.body,
                    "srt": transcript_srt.body,
                    "json": transcript_json.body,
                    "summary_markdown": None,
                    "summary_coverage": None,
                    "clips": (),
                },
            ),
            exported_at=STAMP,
        )
