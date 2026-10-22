"""Synthetic legacy-record coverage for presentation-only export handling."""
from __future__ import annotations

from tests.fixture_write import fixture_write

import copy
import csv
import hashlib
import io
import json
import zipfile
from dataclasses import replace
from types import SimpleNamespace
from xml.etree import ElementTree

import pytest
from fastapi.testclient import TestClient
from markupsafe import Markup

from case_intelligence.generation import GroundedGenerationService
from case_intelligence.managed_storage import StoragePolicy
from case_intelligence.media_evidence import export_transcript
from case_intelligence.workbench import create_workbench_app
from case_intelligence.work_product_exports import (
    ExportBlock,
    ExportProblem,
    blocks_to_docx,
    export_answer,
    export_conversation,
    export_full_review,
    export_matter_bundle,
    export_notebook,
    export_report,
    export_research,
)
from case_intelligence.workspace_store import (
    NotebookItemRecord,
    NotebookReferenceRecord,
    ReportCitationRecord,
    ReportRecord,
    ReportSectionRecord,
    ResearchJobRecord,
)
from tests.test_work_product_exports import (
    STAMP,
    _records,
    _review_records,
    _transcript_segments,
)


LEGACY_TEXT = "before\x1amiddle\x1bafter"
PRESENTED_TEXT = "before middle after"
UNICODE_TEXT = "café العربية فارسی\u200cword 👩\u200d💻 \u200b\u2060"
EXACT_SOURCE_TEXT = " \tbefore\x1acafe\u0301 \t\r\nmiddle \r after\x1b  \n"


def _docx_text(body: bytes) -> str:
    """Parse every XML part, not just ZIP integrity or string containment."""

    with zipfile.ZipFile(io.BytesIO(body)) as archive:
        assert archive.testzip() is None
        parsed = {
            name: ElementTree.fromstring(archive.read(name))
            for name in archive.namelist()
            if name.endswith((".xml", ".rels"))
        }
    return "".join(parsed["word/document.xml"].itertext())


@pytest.mark.parametrize("character", ["\x00", "\x1a", "\x1b", "\x7f", "\x85", "\ud800", "\udfff", "\ufffe", "\uffff"])
def test_docx_serializes_controls_and_invalid_unicode_in_body_and_metadata(character):
    value = f"left{character}right & <literal> {UNICODE_TEXT}"
    expected = f"left right & <literal> {UNICODE_TEXT}"
    block = ExportBlock(value)
    body = blocks_to_docx((block,), title=value, created_at=f"{STAMP}{character}")
    rendered = _docx_text(body)
    assert rendered.startswith(expected)
    assert "replaced with spaces for this export" in rendered
    with zipfile.ZipFile(io.BytesIO(body)) as archive:
        core = ElementTree.fromstring(archive.read("docProps/core.xml"))
        title = core.find("{http://purl.org/dc/elements/1.1/}title")
        assert title is not None and title.text == expected
    assert block.text == value


def test_ordinary_unicode_does_not_receive_a_normalization_notice():
    body = blocks_to_docx((ExportBlock(UNICODE_TEXT),), title=UNICODE_TEXT, created_at=STAMP)
    assert _docx_text(body) == UNICODE_TEXT


@pytest.mark.parametrize("line_ending", ["\r", "\r\n", "\n"])
def test_docx_body_normalizes_line_endings_with_notice_and_metadata_stays_exact(line_ending):
    value = f"before{line_ending}after\t& <literal> &#13;"
    block = ExportBlock(value)
    body = blocks_to_docx((block,), title=value, created_at=STAMP)
    _docx_text(body)  # Check every package XML part before reading its text.
    with zipfile.ZipFile(io.BytesIO(body)) as archive:
        document = ElementTree.fromstring(archive.read("word/document.xml"))
        core = ElementTree.fromstring(archive.read("docProps/core.xml"))
    word = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    paragraph = document.find(f".//{word}p")
    assert paragraph is not None
    # Body line endings become one explicit break and tabs remain explicit;
    # metadata retains its CR via a reference rather than XML normalization.
    round_trip = "".join(
        {word + "br": "\n", word + "tab": "\t"}.get(node.tag, node.text or "")
        for node in paragraph.iter() if node.tag in {word + "t", word + "br", word + "tab"}
    )
    assert round_trip == "before\nafter\t& <literal> &#13;"
    assert len(paragraph.findall(f".//{word}br")) == 1
    assert len(paragraph.findall(f".//{word}tab")) == 1
    title = core.find("{http://purl.org/dc/elements/1.1/}title")
    assert title is not None and title.text == value
    assert block.text == value
    assert "replaced with spaces" not in _docx_text(body)
    assert ("Carriage-return line endings were normalized" in _docx_text(body)) == ("\r" in value)


def test_docx_body_tabs_are_explicit_and_do_not_trigger_a_normalization_notice():
    value = "\tbefore\t\tafter\t"
    body = blocks_to_docx((ExportBlock(value, "title"),), title=value, created_at=STAMP)
    _docx_text(body)
    with zipfile.ZipFile(io.BytesIO(body)) as archive:
        document = ElementTree.fromstring(archive.read("word/document.xml"))
        core = ElementTree.fromstring(archive.read("docProps/core.xml"))
        styles = ElementTree.fromstring(archive.read("word/styles.xml"))
    word = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    paragraph = document.find(f".//{word}p")
    assert paragraph is not None
    assert len(document.findall(f".//{word}p")) == 1
    assert len(paragraph.findall(f".//{word}tab")) == 4
    assert "".join(
        "\t" if node.tag == word + "tab" else node.text or ""
        for node in paragraph.iter() if node.tag in {word + "t", word + "tab"}
    ) == value
    assert all("\t" not in (node.text or "") for node in paragraph.iter(word + "t"))
    title = core.find("{http://purl.org/dc/elements/1.1/}title")
    assert title is not None and title.text == value
    title_style = styles.find(f"{word}style[@{word}styleId='Title']")
    assert title_style is not None
    # The render regression was a title word filling the implicit half-inch
    # tab stop; provide explicit wider stops for the larger title glyphs.
    stops = title_style.findall(f"{word}pPr/{word}tabs/{word}tab")
    assert [stop.attrib[word + "pos"] for stop in stops] == [
        str(position) for position in range(1440, 10081, 1440)
    ]
    assert all(stop.attrib[word + "val"] == "left" for stop in stops)


def test_docx_metadata_carriage_returns_do_not_trigger_body_normalization_notice():
    body = blocks_to_docx((ExportBlock("unchanged body"),), title="before\rafter", created_at=STAMP)
    assert _docx_text(body) == "unchanged body"
    with zipfile.ZipFile(io.BytesIO(body)) as archive:
        core = ElementTree.fromstring(archive.read("docProps/core.xml"))
    title = core.find("{http://purl.org/dc/elements/1.1/}title")
    assert title is not None and title.text == "before\rafter"


@pytest.mark.parametrize("value", ["before\rafter", "before\r\nafter\x1b"])
def test_docx_size_bound_includes_all_normalization_notices(monkeypatch, value):
    from case_intelligence import work_product_exports

    notices = work_product_exports._DOCX_LINE_ENDING_NOTICE
    if "\x1b" in value:
        notices += work_product_exports._TEXT_PRESENTATION_NOTICE
    total = len(value) + len(notices)
    monkeypatch.setattr(work_product_exports, "MAX_EXPORT_TEXT_CHARS", total)
    body = blocks_to_docx((ExportBlock(value),), title="Synthetic title", created_at=STAMP)
    assert "Carriage-return line endings were normalized" in _docx_text(body)
    monkeypatch.setattr(work_product_exports, "MAX_EXPORT_TEXT_CHARS", total - 1)
    with pytest.raises(ExportProblem, match="too large"):
        blocks_to_docx((ExportBlock(value),), title="Synthetic title", created_at=STAMP)


def _legacy_notebook(matter):
    item = NotebookItemRecord(
        item_id="notebook-" + "1" * 32, matter_id=matter.matter_id,
        item_type="note", status="suggested", title=LEGACY_TEXT,
        body=LEGACY_TEXT + " " + UNICODE_TEXT, date_label="", is_pinned=0,
        origin="source", source_conversation_id=None, source_message_id=None,
        created_by="principal-synthetic", created_at=STAMP,
        updated_by="principal-synthetic", updated_at=STAMP,
    )
    reference = NotebookReferenceRecord(
        reference_id="reference-" + "2" * 32, item_id=item.item_id,
        matter_id=matter.matter_id, ordinal=1, document_id="3" * 32,
        source_version_id="4" * 32, source_name="Synthetic controls.txt",
        location="Lines 1–2", unit_number=1, chunk_id="synthetic-chunk",
        excerpt_digest=hashlib.sha256(LEGACY_TEXT.encode()).hexdigest(),
        excerpt=LEGACY_TEXT, support_token="5" * 40, created_at=STAMP,
    )
    return item, (reference,)


@pytest.mark.parametrize("format_name", ["docx", "markdown", "csv"])
def test_legacy_notebook_exports_present_controls_without_changing_reference_basis(format_name):
    matter, *_ = _records()
    entry = _legacy_notebook(matter)
    snapshot = copy.deepcopy(entry)
    artifact = export_notebook(matter, (entry,), format_name, exported_at=STAMP)
    rendered = _docx_text(artifact.body) if format_name == "docx" else artifact.body.decode()
    assert PRESENTED_TEXT in rendered
    assert LEGACY_TEXT not in rendered
    assert UNICODE_TEXT in rendered
    if format_name != "csv":
        assert "replaced with spaces for this export" in rendered
    assert entry == snapshot
    assert entry[1][0].excerpt_digest == hashlib.sha256(LEGACY_TEXT.encode()).hexdigest()


@pytest.mark.parametrize("format_name", ["docx", "markdown"])
def test_legacy_saved_answer_and_conversation_exports(format_name):
    matter, conversation, question, answer = _records()
    answer = replace(answer, payload={**answer.payload, "introduction": LEGACY_TEXT})
    question = replace(question, content=LEGACY_TEXT + " " + UNICODE_TEXT)
    snapshot = copy.deepcopy((question, answer))
    artifacts = (
        export_answer(matter, conversation, answer, question, format_name, exported_at=STAMP),
        export_conversation(matter, conversation, (question, answer), format_name, exported_at=STAMP),
    )
    for artifact in artifacts:
        rendered = _docx_text(artifact.body) if format_name == "docx" else artifact.body.decode()
        assert PRESENTED_TEXT in rendered and LEGACY_TEXT not in rendered
        assert UNICODE_TEXT in rendered
    assert (question, answer) == snapshot


def test_legacy_report_docx_preserves_saved_section_and_citation():
    matter, *_ = _records()
    report = ReportRecord(
        report_id="report-" + "1" * 32, matter_id=matter.matter_id,
        title=LEGACY_TEXT, purpose="Synthetic export regression", status="draft",
        created_by="principal-synthetic", created_at=STAMP,
        updated_by="principal-synthetic", updated_at=STAMP,
    )
    section = ReportSectionRecord(
        section_id="section-" + "2" * 32, report_id=report.report_id,
        matter_id=matter.matter_id, ordinal=1, heading=LEGACY_TEXT,
        body=LEGACY_TEXT + " " + UNICODE_TEXT, origin="staff", origin_id="",
        created_by="principal-synthetic", created_at=STAMP,
        updated_by="principal-synthetic", updated_at=STAMP,
    )
    citation = ReportCitationRecord(
        citation_id="citation-" + "3" * 32, section_id=section.section_id,
        report_id=report.report_id, matter_id=matter.matter_id, ordinal=1,
        kind="source", document_id="4" * 32, source_version_id="5" * 32,
        source_name="Synthetic controls.txt", location="Lines 1–2",
        support_token="6" * 40, excerpt=LEGACY_TEXT, media_clip_id="",
        start_ms=0, end_ms=0, created_at=STAMP,
    )
    snapshot = copy.deepcopy((report, section, citation))
    artifact = export_report(matter, report, ((section, (citation,)),), "docx", exported_at=STAMP)
    rendered = _docx_text(artifact.body)
    assert PRESENTED_TEXT in rendered and UNICODE_TEXT in rendered
    assert (report, section, citation) == snapshot


def test_legacy_every_source_review_and_transcript_docx():
    matter, *_ = _records()
    criterion, version, run, decision, metrics = _review_records(matter)
    decision = replace(decision, rationale=LEGACY_TEXT + " " + UNICODE_TEXT)
    segments = tuple(replace(item, current_text=LEGACY_TEXT) for item in _transcript_segments(matter))
    snapshot = copy.deepcopy((decision, segments))
    artifacts = (
        export_full_review(matter, criterion, version, run, (decision,), metrics, "docx", exported_at=STAMP),
        export_transcript("Synthetic controls.wav", segments, "docx"),
    )
    for artifact in artifacts:
        assert PRESENTED_TEXT in _docx_text(artifact.body)
    assert (decision, segments) == snapshot


def _investigation_job(matter, excerpt=LEGACY_TEXT):
    reference = _legacy_notebook(matter)[1][0]
    evidence = {
        "matter_id": matter.matter_id, "support_token": reference.support_token,
        "document_id": reference.document_id, "source_version_id": reference.source_version_id,
        "source_name": reference.source_name, "location": reference.location,
        "chunk_id": reference.chunk_id, "unit_number": reference.unit_number,
        "line_start": 1, "line_end": 2, "excerpt": excerpt,
        "excerpt_digest": hashlib.sha256(excerpt.encode()).hexdigest(), "evidence_kind": "document",
    }
    answer = {"answerable": True, "introduction": LEGACY_TEXT, "claims": [], "source_matches": [evidence]}
    result = {"evidence": [evidence], "answer": answer, "summary": LEGACY_TEXT, "passes": []}
    return ResearchJobRecord(
        job_id="research-" + "7" * 32, matter_id=matter.matter_id,
        actor_id="principal-synthetic", idempotency_key="synthetic-export",
        question="What does the synthetic text say?", title="Synthetic investigation",
        source_set_id=None, conversation_id=None, result_message_id=None,
        state="succeeded", stage="complete", attempts=1, worker_id=None,
        cancellation_requested=0, plan={}, result=result, total_steps=1,
        completed_steps=1, candidate_count=1, evidence_count=1, message="Complete",
        created_at=STAMP, started_at=STAMP, last_claimed_at=STAMP,
        finished_at=STAMP, updated_at=STAMP,
    )


def test_legacy_investigation_docx_keeps_verified_source_basis():
    matter, *_ = _records()
    job = _investigation_job(matter)
    evidence = job.result["evidence"][0]
    snapshot = copy.deepcopy(job)
    assert PRESENTED_TEXT in _docx_text(export_research(matter, job, "docx", exported_at=STAMP).body)
    assert job == snapshot
    assert evidence["excerpt_digest"] == hashlib.sha256(evidence["excerpt"].encode()).hexdigest()


@pytest.mark.parametrize("excerpt", [LEGACY_TEXT, EXACT_SOURCE_TEXT])
def test_bundle_parses_all_nested_word_exports_and_keeps_json_source_excerpt(excerpt):
    matter, conversation, question, answer = _records()
    question = replace(question, content=LEGACY_TEXT)
    item, references = _legacy_notebook(matter)
    entry = (item, (replace(references[0], excerpt=excerpt,
                           excerpt_digest=hashlib.sha256(excerpt.encode()).hexdigest()),))
    snapshot = copy.deepcopy(entry)
    artifact = export_matter_bundle(
        matter, ((conversation, (question, answer)),), (), (entry,), exported_at=STAMP,
    )
    with zipfile.ZipFile(io.BytesIO(artifact.body)) as archive:
        docx_names = [name for name in archive.namelist() if name.endswith(".docx")]
        assert len(docx_names) == 3
        for name in docx_names:
            _docx_text(archive.read(name))
        notebook = json.loads(archive.read("notebook/notebook.json"))
        assert notebook["items"][0]["sources"][0]["excerpt"] == excerpt
        assert PRESENTED_TEXT in archive.read("notebook/matter-notebook.csv").decode()
    assert entry == snapshot


def test_every_source_json_retains_exact_whitespace_cr_and_decomposed_unicode():
    matter, *_ = _records()
    criterion, version, run, decision, metrics = _review_records(matter)
    citation = {**decision.citations[0], "excerpt": EXACT_SOURCE_TEXT,
                "excerpt_digest": hashlib.sha256(EXACT_SOURCE_TEXT.encode()).hexdigest()}
    decision = replace(decision, citations=(citation,))
    snapshot = copy.deepcopy(decision)
    artifact = export_full_review(matter, criterion, version, run, (decision,), metrics,
                                  "json", exported_at=STAMP)
    excerpt = json.loads(artifact.body)["decisions"][0]["citations"][0]["excerpt"]
    assert excerpt == EXACT_SOURCE_TEXT
    assert hashlib.sha256(excerpt.encode()).hexdigest() == citation["excerpt_digest"]
    assert decision == snapshot


def test_investigation_json_retains_exact_source_text_at_every_citation_path():
    matter, *_ = _records()
    job = _investigation_job(matter, EXACT_SOURCE_TEXT)
    evidence = job.result["evidence"][0]
    answer = {"answerable": True, "introduction": "Synthetic source finding.",
              "claims": [{"text": "The source records before and after.", "citations": [evidence]}],
              "limitation": {"text": "The observation remains limited.", "citations": [evidence]}}
    summary = "Synthetic source finding.\nThe source records before and after.\nLimitation: The observation remains limited."
    result = {**job.result, "answer": answer, "summary": summary,
              "passes": [{"query": "before", "status": "supported", "text": summary,
                          "answer": answer, "motivating_support_token": evidence["support_token"]}],
              "pending_searches": [{"query": "after", "support_token": evidence["support_token"]}]}
    job = replace(job, result=result)
    snapshot = copy.deepcopy(job)
    exported = json.loads(export_research(matter, job, "json", exported_at=STAMP).body)["investigation"]
    citations = [
        exported["supporting_sources"][0], exported["answer"]["claims"][0]["sources"][0],
        exported["answer"]["limitation"]["sources"][0], exported["findings"][0]["motivating_source"],
        exported["unsearched_proposals"][0]["motivating_source"],
    ]
    assert all(citation["excerpt"] == EXACT_SOURCE_TEXT for citation in citations)
    assert all(hashlib.sha256(citation["excerpt"].encode()).hexdigest() == evidence["excerpt_digest"]
               for citation in citations)
    assert job == snapshot


@pytest.mark.parametrize("changed_excerpt", [
    EXACT_SOURCE_TEXT.strip(),
    EXACT_SOURCE_TEXT.replace("\r\n", "\n"),
    EXACT_SOURCE_TEXT.replace("\r after", "\n after"),
])
def test_investigation_export_refuses_normalization_equivalent_citation_excerpt(changed_excerpt):
    matter, *_ = _records()
    original = _investigation_job(matter, EXACT_SOURCE_TEXT)
    original_snapshot = copy.deepcopy(original)
    valid = json.loads(export_research(matter, original, "json", exported_at=STAMP).body)
    assert valid["investigation"]["supporting_sources"][0]["excerpt"] == EXACT_SOURCE_TEXT
    citation = {**original.result["evidence"][0], "excerpt": changed_excerpt}
    changed = replace(original, result={**original.result, "answer": {
        **original.result["answer"], "source_matches": [citation],
    }})
    changed_snapshot = copy.deepcopy(changed)
    with pytest.raises(ExportProblem, match="citation did not match its evidence ledger"):
        export_research(matter, changed, "json", exported_at=STAMP)
    assert original == original_snapshot and changed == changed_snapshot


@pytest.mark.parametrize("excerpt_presence", ["missing", "null", "empty"])
def test_investigation_export_keeps_legacy_missing_excerpt_compatibility(excerpt_presence):
    matter, *_ = _records()
    original = _investigation_job(matter, EXACT_SOURCE_TEXT)
    citation = dict(original.result["evidence"][0])
    if excerpt_presence == "missing":
        citation.pop("excerpt")
    else:
        citation["excerpt"] = None if excerpt_presence == "null" else ""
    job = replace(original, result={**original.result, "answer": {
        **original.result["answer"], "source_matches": [citation],
    }})
    snapshot = copy.deepcopy(job)
    exported = json.loads(export_research(matter, job, "json", exported_at=STAMP).body)
    assert exported["investigation"]["supporting_sources"][0]["excerpt"] == EXACT_SOURCE_TEXT
    assert job == snapshot


def test_structured_excerpt_size_limit_rejects_instead_of_truncating(monkeypatch):
    matter, *_ = _records()
    criterion, version, run, decision, metrics = _review_records(matter)
    citation = {**decision.citations[0], "excerpt": EXACT_SOURCE_TEXT}
    decision = replace(decision, citations=(citation,))
    monkeypatch.setattr("case_intelligence.work_product_exports.MAX_WORKFLOW_EXPORT_BYTES",
                        len(EXACT_SOURCE_TEXT.encode()) - 1)
    with pytest.raises(ExportProblem, match="source excerpt exceeds the export limit"):
        export_full_review(matter, criterion, version, run, (decision,), metrics, "json", exported_at=STAMP)
    assert citation["excerpt"] == EXACT_SOURCE_TEXT


def test_csv_formula_guard_runs_after_control_replacement():
    matter, *_ = _records()
    item, references = _legacy_notebook(matter)
    item = replace(item, title="\x1a=1+1")
    artifact = export_notebook(matter, ((item, references),), "csv", exported_at=STAMP)
    row = next(csv.DictReader(io.StringIO(artifact.body.decode("utf-8-sig"))))
    assert row["Title"] == "' =1+1"


def test_csv_preserves_bare_carriage_return_word_boundaries_without_rewriting_record():
    matter, *_ = _records()
    item, references = _legacy_notebook(matter)
    raw = "left\rright\r\nnext"
    item = replace(item, body=raw)
    artifact = export_notebook(matter, ((item, references),), "csv", exported_at=STAMP)
    row = next(csv.DictReader(io.StringIO(artifact.body.decode("utf-8-sig"))))
    assert row["Details"] == "left\nright\nnext"
    assert item.body == raw


def test_http_legacy_note_presentation_keeps_html_escaping_and_stored_text(tmp_path):
    actor = "development-taylor-morgan"
    raw = 'left\x1aright <script>alert("synthetic")</script> & <tag> ' + UNICODE_TEXT
    with TestClient(create_workbench_app(tmp_path / "runtime", auth_mode="test")) as client:
        bench = client.app.state.workbench
        matter = bench.create_matter("Synthetic escaping regression", "", actor)
        response = client.post(f"/matters/{matter.slug}/notebook/items", data={
            "item_type": "note", "status": "needs_review", "title": "Synthetic literal text",
            "body": raw,
        }, follow_redirects=False)
        assert response.status_code == 303
        item = bench.workspace.all_notebook_items(matter.matter_id, actor)[0]
        # Emulate an older saved row; rendering must not rewrite its text.
        with fixture_write(bench.workspace):
            bench.workspace.connection.execute(
                "UPDATE workbench_notebook_item SET body=? WHERE item_id=?", (raw, item.item_id),
            )
        page = client.get(f"/matters/{matter.slug}/notebook")
        assert page.status_code == 200
        assert "left right &lt;script&gt;alert(" in page.text
        assert "&lt;/script&gt; &amp; &lt;tag&gt;" in page.text
        assert '<script>alert("synthetic")</script>' not in page.text
        assert "&amp;lt;script&amp;gt;" not in page.text
        assert UNICODE_TEXT in page.text and "\x1a" not in page.text
        assert bench.workspace.notebook_item(matter.matter_id, actor, item.item_id).body == raw
        # Jinja's trusted, already-escaped markup keeps its existing boundary.
        template = page.template.environment.from_string("{{ value }}")
        assert template.render(value=Markup("<em>left\x1aright</em>")) == "<em>left right</em>"


def test_full_text_review_exports_hydrate_controls_without_rewriting_frozen_ledger(tmp_path):
    actor = "development-taylor-morgan"
    raw = LEGACY_TEXT + " " + UNICODE_TEXT
    app = create_workbench_app(tmp_path / "runtime", auth_mode="test",
                              storage_policy=StoragePolicy(reserve_bytes=0))
    with TestClient(app) as client:
        bench = app.state.workbench
        for coordinator in (bench.answers, bench.research, bench.full_review):
            coordinator.close()
        matter = bench.create_matter("Synthetic full text controls", "", actor)
        sources = bench.source_store(matter)
        document, _ = sources.store_stream("Synthetic controls.txt", "text/plain", io.BytesIO(raw.encode()))
        bench._sync_source_catalog(matter, [document])
        _, version = bench.workspace.create_review_criterion(
            matter.matter_id, actor, title="Find the synthetic text",
            instructions="Include the passage that mentions before and after.",
        )
        queued = bench.workspace.queue_review_run(
            matter.matter_id, actor, version.criterion_version_id,
            run_kind="full", review_mode="full_text",
        )
        run = bench.workspace.claim_review_run("synthetic-full-text-worker")
        assert run.run_id == queued.run_id
        decision = bench.workspace.next_review_decision(run.run_id)
        bench.generator = GroundedGenerationService(SimpleNamespace(
            available=True,
            classify_source=lambda **kwargs: {
                "decision": "include", "rationale": kwargs["evidence"][0].excerpt,
                "evidence_ids": ["S1"],
            },
        ))
        outcome = bench._process_review_decision(run, decision, lambda: False)
        assert outcome.decision == "included"
        bench._record_review_decision(run, decision, outcome)
        bench._finish_review_run(run)
        before = bench.workspace.review_decisions_for_export(matter.matter_id, actor, run.run_id)

        # The HTTP export hydrates compact full-text locators from the exact
        # frozen source before passing that derived copy to the DOCX serializer.
        route = f"/matters/{matter.slug}/full-review/{run.run_id}"
        response = client.get(route + "/export?format=docx")
        assert response.status_code == 200, response.text
        rendered = _docx_text(response.content)
        assert PRESENTED_TEXT in rendered and UNICODE_TEXT in rendered
        assert LEGACY_TEXT not in rendered
        portable = client.get(route + "/export?format=json")
        assert portable.status_code == 200
        assert portable.json()["decisions"][0]["citations"][0]["excerpt"] == raw

        ledger_response = client.get(route + "/text/export?format=json")
        assert ledger_response.status_code == 200
        records = ledger_response.json()["records"]
        unit = next(row for row in records if row["record_type"] == "unit")
        saved_range = next(row for row in records if row["record_type"] == "range")
        assert unit["unit_digest"] == hashlib.sha256(raw.encode()).hexdigest()
        assert saved_range["rationale"] == raw
        assert (saved_range["coverage_start"], saved_range["coverage_end"]) == (0, len(raw))
        ledger_csv = client.get(route + "/text/export?format=csv")
        assert ledger_csv.status_code == 200
        csv_range = next(row for row in csv.DictReader(io.StringIO(ledger_csv.text))
                         if row["Record type"] == "range")
        assert json.loads(csv_range["Saved record JSON"])["rationale"] == raw
        assert bench.workspace.review_decisions_for_export(matter.matter_id, actor, run.run_id) == before
        assert sources.source_path(document.document_id, verify_digest=True).read_bytes() == raw.encode()
