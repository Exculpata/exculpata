"""New synthetic Pump Cedar regressions through answer persistence and Reports.

The generated PCM carrier and deterministic processor exercise media ingestion,
not speech recognition. The answer client echoes retrieved text so the tests
qualify exact support persistence rather than model quality.
"""
from __future__ import annotations

import io
import threading
import time
import wave
import zipfile

from fastapi.testclient import TestClient
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
import pytest

from case_intelligence.generation import GroundedGenerationService
from case_intelligence.report_materials import snapshot_report_materials
from case_intelligence.workbench import create_workbench_app
from case_intelligence.workspace_store import WorkspaceProblem, WorkspaceStore
from tests.test_guided_reports import finished, queue
from tests.test_matter_media_workflow import ImmediateMediaProcessor

ACTOR = "development-taylor-morgan"
INSPECTION = (
    "During a training exercise, Pump Cedar showed an 18 psi reading while a "
    "reference gauge showed 12 psi. The operator paused the exercise. "
    "The reason for the difference was not yet established."
)
RECOLLECTION = (
    "I saw the operator stop the exercise after comparing two readings. "
    "I did not watch the sensor replacement or any later measurement."
)
QUESTION = "What can we establish about the discrepancy, what happened afterward, and what remains unverified?"


def inspection_pdf():
    writer = PdfWriter()
    page = writer.add_blank_page(width=612, height=792)
    font = DictionaryObject({NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/Type1"), NameObject("/BaseFont"): NameObject("/Helvetica")})
    page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"):
        DictionaryObject({NameObject("/F1"): writer._add_object(font)})})
    stream = DecodedStreamObject()
    lines = [INSPECTION[index:index + 70] for index in range(0, len(INSPECTION), 70)]
    stream.set_data(("BT /F1 12 Tf 36 750 Td 16 TL " + " ".join(
        "(" + line + ") Tj T*" for line in lines) + " ET").encode())
    page[NameObject("/Contents")] = writer._add_object(stream)
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def synthetic_pcm():
    output = io.BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setparams((1, 2, 8000, 0, "NONE", "not compressed"))
        audio.writeframes(b"\x00\x00" * 8000 * 3)
    return output.getvalue()


class CedarProcessor(ImmediateMediaProcessor):
    def transcript(self, owner, external_job_id):
        result = super().transcript(owner, external_job_id)
        segment = {**result["segments"][0], "start": 0.5, "end": 2.5,
                   "text": RECOLLECTION, "model_text": RECOLLECTION}
        return {**result, "segments": [segment]}


class RetrievedTextClient:
    available = True

    def generate(self, *, evidence, **kwargs):
        return {"answerable": bool(evidence), "claims": [
            {"text": (("The machine transcript appears to say that " if item.evidence_kind == "transcript" else "")
                      + (item.excerpt.split(". ", 1)[0] + "." if item.evidence_kind == "transcript" else item.excerpt)),
             "evidence_ids": [item.evidence_id]}
            for item in evidence], "limitation": None, "missing_information": ""}


@pytest.fixture
def cedar(tmp_path, monkeypatch):
    monkeypatch.setenv("CASE_INTELLIGENCE_STORAGE_RESERVE_GIB", "0")
    app = create_workbench_app(tmp_path / "runtime", generator=RetrievedTextClient(),
        auth_mode="test", media_processor=CedarProcessor(), media_poll_seconds=.01)
    with TestClient(app) as client:
        bench = app.state.workbench
        bench.answers.close()
        bench.research.close()
        response = client.post("/matters", data={"name": "Synthetic Pump Cedar support"}, follow_redirects=False)
        matter = bench.matter(response.headers["location"].split("/")[2], ACTOR)
        for upload in [("Inspection note.pdf", inspection_pdf(), "application/pdf"),
                       ("Staff recollection recording.wav", synthetic_pcm(), "audio/wav")]:
            uploaded = client.post(f"/matters/{matter.slug}/uploads", files=[("files", upload)], follow_redirects=False)
            assert uploaded.status_code == 303, uploaded.text
        store = bench.source_store(matter)
        deadline = time.monotonic() + 8
        continued_inspections = set()
        while time.monotonic() < deadline:
            documents = tuple(store.documents.values())
            for document in documents:
                if document.media_type == "audio/wav" and document.state == "needs_review":
                    job = bench.workspace.media_job(matter.matter_id, document.document_id, document.version_id)
                    inspection_id = job.preflight.get("inspection_id") if job else None
                    if (job and job.state == "cancelled" and inspection_id
                            and inspection_id not in continued_inspections):
                        # The silent PCM carrier deliberately needs the supported
                        # human decision once its durable inspection is held.
                        # The source can still say needs_review after requeue.
                        response = client.post(f"/matters/{matter.slug}/sources/{store.action_token(document)}/recording-check",
                            data={"action": "continue", "inspection_id": inspection_id}, follow_redirects=False)
                        assert response.status_code == 303, response.text
                        continued_inspections.add(inspection_id)
            readiness = bench.workspace.matter_readiness(matter.matter_id)
            if (len(documents) == 2 and all(document.state == "ready" for document in documents)
                    and readiness.can_query and readiness.searchable_count == 2):
                break
            time.sleep(.01)
        assert (len(documents) == 2 and all(document.state == "ready" for document in documents)
                and readiness.can_query and readiness.searchable_count == 2), (
            [(d.display_name, d.state, d.message) for d in documents], readiness,
        )
        documents = {"transcript" if doc.media_type == "audio/wav" else "pdf": doc for doc in documents}
        assert "18 psi" in documents["pdf"].parsed_units()[0].text
        assert RECOLLECTION in documents["transcript"].parsed_units()[0].text
        yield client, bench, matter, documents


def save_answer(cedar, monkeypatch, kinds, *, queued=False):
    _, bench, matter, documents = cedar
    conversation = bench.workspace.get_conversation(matter.matter_id)
    citations = tuple(bench._citation(matter, bench._candidate(matter, documents[kind],
        documents[kind].parsed_units()[0], 1)) for kind in kinds)
    # Fixed retrieval population isolates the producer from relevance/model variability.
    monkeypatch.setattr(bench, "_answer_search", lambda *args, **kwargs: citations)
    if queued:
        job, _ = bench.queue_answer(matter, conversation, QUESTION, "answer-request-" + "a" * 32, ACTOR)
        claimed = bench.workspace.claim_answer_job("synthetic-cedar-worker")
        assert claimed.job_id == job.job_id
        result = bench._process_answer_job(claimed, lambda *args: None, lambda: False)
        message = bench._finish_answer_job(claimed, result)
        assert bench.workspace.get_answer_job(matter.matter_id, ACTOR, job.job_id).state == "succeeded"
    else:
        message = bench.ask(matter, conversation, QUESTION)
    assert message.payload["kind"] == "generated"
    assert len(message.payload["claims"]) == len(kinds)
    return conversation, message, citations


def test_fixture_continues_each_recording_check_only_once(request, monkeypatch):
    continued = threading.Event()
    release = threading.Event()
    observed = []
    decisions = []
    claim = WorkspaceStore.claim_media_job
    media_job = WorkspaceStore.media_job
    decide = WorkspaceStore.decide_media_preflight

    def hold_requeued_worker(workspace, *args, **kwargs):
        # The store lock covers the gate and the claim together. Checking the
        # event first lets a claim already inside the store pick up the requeue.
        with workspace._lock:
            if continued.is_set() and not release.is_set():
                return None
            return claim(workspace, *args, **kwargs)

    def continue_once(workspace, *args, **kwargs):
        with workspace._lock:
            continued.set()
            job = decide(workspace, *args, **kwargs)
            decisions.append(job.media_job_id)
            return job

    def observe_queued_decision(workspace, *args, **kwargs):
        job = media_job(workspace, *args, **kwargs)
        if (continued.is_set() and not release.is_set()
                and job is not None and job.state == "queued"):
            observed.append(job)
            release.set()
        return job

    monkeypatch.setattr(WorkspaceStore, "claim_media_job", hold_requeued_worker)
    monkeypatch.setattr(WorkspaceStore, "decide_media_preflight", continue_once)
    monkeypatch.setattr(WorkspaceStore, "media_job", observe_queued_decision)
    try:
        _, bench, matter, documents = request.getfixturevalue("cedar")
        assert len(decisions) == len(observed) == 1
        assert observed[0].preflight["continued"] is True
        assert documents["transcript"].state == "ready"
        assert bench.workspace.matter_readiness(matter.matter_id).can_query
    finally:
        release.set()


@pytest.mark.parametrize("kinds,queued", [(("pdf",), False), (("transcript",), False),
    (("pdf", "transcript"), False), (("pdf", "transcript"), True)])
def test_saved_answer_compiles_with_exact_cited_sources_and_exports(cedar, monkeypatch, kinds, queued):
    client, bench, matter, documents = cedar
    conversation, message, citations = save_answer(cedar, monkeypatch, kinds, queued=queued)
    # Read through a separate connection to prove the answer was persisted.
    reopened = WorkspaceStore(bench.workspace.path)
    try:
        saved = reopened.messages(matter.matter_id, conversation.conversation_id)[-1]
        assert saved == message
    finally:
        reopened.close()
    for citation in citations:
        opened = client.get(citation.href)
        assert opened.status_code == 200 and citation.source_name in opened.text
        support = bench.support(matter, citation.support_token)
        assert support.location == citation.location
    result = finished(client, matter, queue(client, matter, [f"conversation:{conversation.conversation_id}"]))
    assert result["state"] == "succeeded", result
    report = bench.workspace.reports(matter.matter_id, ACTOR)[0]
    references = [ref for section in bench.workspace.report_sections(matter.matter_id, report.report_id)
        for ref in bench.workspace.report_citations(matter.matter_id, report.report_id, section.section_id)]
    for original in citations:
        frozen = next(value for value in references if value.support_token == original.support_token)
        assert (frozen.document_id, frozen.source_version_id, frozen.location, frozen.excerpt) == (
            original.document_id, original.source_version_id, original.location, original.excerpt)
        saved_ref = next(ref for claim in saved.payload["claims"] for ref in claim["citations"]
                         if ref["support_token"] == original.support_token)
        expected = bench._workflow_citation_payload(original)
        expected.pop("excerpt")
        assert saved_ref == expected
    for fmt in ("markdown", "docx"):
        response = client.get(f"/matters/{matter.slug}/reports/{report.report_id}/export?format={fmt}")
        assert response.status_code == 200, response.text[:100]
        if fmt == "docx":
            with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
                exported = archive.read("word/document.xml").decode()
        else:
            exported = response.text
        for citation in citations:
            assert citation.source_name in exported and citation.location in exported
        if "transcript" in kinds:
            assert RECOLLECTION in exported


@pytest.mark.parametrize("kind", ["pdf", "transcript"])
def test_individually_saved_supported_answer_note_compiles(cedar, monkeypatch, kind):
    client, bench, matter, _ = cedar
    conversation, message, _ = save_answer(cedar, monkeypatch, (kind,))
    note, _ = bench.save_answer_to_notebook(matter, ACTOR, conversation.conversation_id, message.message_id, 0)
    result = finished(client, matter, queue(client, matter, [f"note:{note.item_id}"]))
    assert result["state"] == "succeeded", result


def edit_transcript(cedar):
    client, bench, matter, documents = cedar
    document = documents["transcript"]
    segment = bench.workspace.transcript_segments(matter.matter_id, document.document_id, document.version_id)[0]
    revised = "The fictional speaker now says only that a training exercise occurred."
    response = client.post(
        f"/matters/{matter.slug}/sources/{bench.source_store(matter).action_token(document)}/segments/{segment.segment_id}",
        data={"expected_revision": segment.current_revision, "text": revised}, follow_redirects=False)
    assert response.status_code == 303 and "error=" not in response.headers["location"], response.text
    current = bench.source_store(matter).get(document.document_id)
    assert revised in current.parsed_units()[0].text
    return revised


def test_transcript_edit_preserves_history_and_blocks_old_conversation_note_and_report_exports(cedar, monkeypatch):
    client, bench, matter, _ = cedar
    conversation, message, citations = save_answer(cedar, monkeypatch, ("pdf", "transcript"))
    note, _ = bench.save_answer_to_notebook(matter, ACTOR, conversation.conversation_id, message.message_id, 1)
    result = finished(client, matter, queue(client, matter, [f"conversation:{conversation.conversation_id}"]))
    assert result["state"] == "succeeded", result
    report = bench.workspace.reports(matter.matter_id, ACTOR)[0]
    original = citations[1]
    revised = edit_transcript(cedar)
    assert bench.support(matter, original.support_token).location == original.location
    assert bench.workspace.messages(matter.matter_id, conversation.conversation_id)[-1] == message
    for selection in (f"conversation:{conversation.conversation_id}", f"note:{note.item_id}"):
        with pytest.raises(WorkspaceProblem, match="source support changed"):
            with bench.source_store(matter).mutation_guard(), bench.workspace._lock:
                snapshot_report_materials(bench, matter, ACTOR, (selection,))
    for fmt in ("markdown", "docx"):
        exported = client.get(f"/matters/{matter.slug}/reports/{report.report_id}/export?format={fmt}")
        assert exported.status_code == 400
        assert revised.encode() not in exported.content
    assert bench.workspace.report_sections(matter.matter_id, report.report_id)


@pytest.mark.parametrize("destination", ["claim_note", "citation_note", "report"])
def test_stale_saved_answer_cannot_be_recaptured_with_current_transcript_text(cedar, monkeypatch, destination):
    _, bench, matter, _ = cedar
    conversation, message, _ = save_answer(cedar, monkeypatch, ("transcript",))
    edit_transcript(cedar)
    with pytest.raises(WorkspaceProblem, match="source support changed"):
        if destination == "report":
            report = bench.workspace.create_report(matter.matter_id, ACTOR, "Synthetic stale-support destination")
            bench.add_answer_to_report(matter, ACTOR, report.report_id, conversation.conversation_id,
                message.message_id, expected_status=report.status)
        else:
            bench.save_answer_to_notebook(matter, ACTOR, conversation.conversation_id, message.message_id, 0,
                citation_index=0 if destination == "citation_note" else None)
    assert not bench.workspace.all_notebook_items(matter.matter_id, ACTOR)


@pytest.mark.parametrize("basis", ["legacy_digest_token", "frozen_digest", "unbound_moment_token"])
def test_persisted_transcript_compatibility_is_exact_and_recovery_keeps_old_work(cedar, monkeypatch, basis):
    client, bench, matter, documents = cedar
    conversation, produced, citations = save_answer(cedar, monkeypatch, ("transcript",))
    original = citations[0]
    reference = original.staff_payload()
    if basis == "legacy_digest_token":
        document = documents["transcript"]
        candidate = bench._candidate(matter, document, document.parsed_units()[0], 1)
        token = bench._legacy_support_token(candidate)
        reference.update(support_token=token, href=original.href.replace(original.support_token, token))
    elif basis == "frozen_digest":
        reference["excerpt_digest"] = original.excerpt_digest
    compatibility = bench.workspace.create_conversation(matter.matter_id, "Synthetic saved-format compatibility")
    payload = {**produced.payload, "claims": [{**produced.payload["claims"][0], "citations": [reference]}]}
    saved = bench.workspace.append_message(matter.matter_id, compatibility.conversation_id, "assistant", produced.content, payload)
    selection = (f"conversation:{compatibility.conversation_id}",)
    if basis == "unbound_moment_token":
        with pytest.raises(WorkspaceProblem, match="new conversation"):
            with bench.source_store(matter).mutation_guard(), bench.workspace._lock:
                snapshot_report_materials(bench, matter, ACTOR, selection)
        with pytest.raises(WorkspaceProblem, match="new conversation"):
            bench.save_answer_to_notebook(matter, ACTOR, compatibility.conversation_id, saved.message_id, 0)
    else:
        with bench.source_store(matter).mutation_guard(), bench.workspace._lock:
            materials = snapshot_report_materials(bench, matter, ACTOR, selection)
        assert materials[0].citations[0]["excerpt"] == original.excerpt
        note, _ = bench.save_answer_to_notebook(matter, ACTOR, compatibility.conversation_id, saved.message_id, 0)
        assert bench.workspace.notebook_references(matter.matter_id, ACTOR, note.item_id)[0].excerpt == original.excerpt
    edit_transcript(cedar)
    with pytest.raises(WorkspaceProblem, match="source support changed"):
        with bench.source_store(matter).mutation_guard(), bench.workspace._lock:
            snapshot_report_materials(bench, matter, ACTOR, selection)
    assert bench.workspace.messages(matter.matter_id, compatibility.conversation_id)[-1] == saved
    # Explicit recovery makes new work from the reviewed current source. The
    # historical conversation is retained and deliberately not selected.
    fresh = bench.workspace.create_conversation(matter.matter_id, "Reviewed Pump Cedar follow-up")
    current = bench.source_store(matter).get(documents["transcript"].document_id)
    citation = bench._citation(matter, bench._candidate(matter, current, current.parsed_units()[0], 1))
    monkeypatch.setattr(bench, "_answer_search", lambda *args, **kwargs: (citation,))
    answer = bench.ask(matter, fresh, QUESTION)
    assert answer.payload["kind"] == "generated"
    result = finished(client, matter, queue(client, matter, [f"conversation:{fresh.conversation_id}"]))
    assert result["state"] == "succeeded", result
    assert bench.workspace.messages(matter.matter_id, compatibility.conversation_id)[-1] == saved


@pytest.mark.parametrize("field,replacement", [
    ("matter_id", "ci-matter-" + "f" * 32), ("document_id", "f" * 32),
    ("source_version_id", "f" * 32), ("source_name", "Synthetic other recording.wav"),
    ("location", "00:12–00:13"), ("excerpt_digest", "f" * 64),
    ("line_start", 12000), ("excerpt", "A different synthetic observation."),
])
def test_saved_answer_support_rejects_forged_identity_version_location_and_text(cedar, monkeypatch, field, replacement):
    _, bench, matter, _ = cedar
    conversation, message, _ = save_answer(cedar, monkeypatch, ("transcript",))
    original = message.payload["claims"][0]
    reference = {**original["citations"][0], field: replacement}
    payload = {**message.payload, "claims": [{**original, "citations": [reference]}]}
    saved = bench.workspace.append_message(matter.matter_id, conversation.conversation_id, "assistant", message.content, payload)
    with pytest.raises(WorkspaceProblem, match="source support changed"):
        bench.save_answer_to_notebook(matter, ACTOR, conversation.conversation_id, saved.message_id, 0)
    with pytest.raises(WorkspaceProblem, match="source support changed"):
        with bench.source_store(matter).mutation_guard(), bench.workspace._lock:
            snapshot_report_materials(bench, matter, ACTOR, (f"conversation:{conversation.conversation_id}",))


def test_saved_answer_capture_rechecks_actor_and_matter_scope(cedar, monkeypatch):
    _, bench, matter, _ = cedar
    conversation, message, _ = save_answer(cedar, monkeypatch, ("transcript",))
    with monkeypatch.context() as isolated:
        isolated.setattr(bench, "source_store", lambda *args: pytest.fail("Read source before checking membership"))
        with pytest.raises(KeyError):
            bench.save_answer_to_notebook(matter, "synthetic-unassigned-reviewer", conversation.conversation_id, message.message_id, 0)
    other = bench.workspace.create_matter("Synthetic other matter", "Scope control", ACTOR)
    with pytest.raises(KeyError):
        bench.save_answer_to_notebook(other, ACTOR, conversation.conversation_id, message.message_id, 0)
    assert not bench.workspace.all_notebook_items(matter.matter_id, ACTOR)


@pytest.mark.parametrize("changed", [False, True])
@pytest.mark.parametrize("legacy", [False, True])
def test_direct_report_retains_and_revalidates_independently_sourced_qualification(cedar, monkeypatch, changed, legacy):
    _, bench, matter, documents = cedar

    class QualifiedClient:
        available = True

        def generate(self, *, evidence, **kwargs):
            written = next(value for value in evidence if value.evidence_kind == "document")
            spoken = next(value for value in evidence if value.evidence_kind == "transcript")
            return {"answerable": True,
                "claims": [{"text": written.excerpt, "evidence_ids": [written.evidence_id]}],
                "limitation": {"text": "The machine transcript appears to say that " + RECOLLECTION.split(". ", 1)[1],
                               "evidence_ids": [spoken.evidence_id]}, "missing_information": ""}

    bench.generator = GroundedGenerationService(QualifiedClient())
    citations = tuple(bench._citation(matter, bench._candidate(matter, document, document.parsed_units()[0], 1))
                      for document in documents.values())
    monkeypatch.setattr(bench, "_answer_search", lambda *args, **kwargs: citations)
    conversation = bench.workspace.get_conversation(matter.matter_id)
    message = bench.ask(matter, conversation, QUESTION)
    assert len(message.payload["claims"]) == 1 and message.payload["source_limitation"]
    if legacy:
        payload = dict(message.payload)
        payload.pop("source_limitation")
        payload.pop("verification_notice")
        message = bench.workspace.append_message(matter.matter_id, conversation.conversation_id, "assistant", message.content, payload)
    if changed:
        edit_transcript(cedar)
    report = bench.workspace.create_report(matter.matter_id, ACTOR, "Synthetic qualified Report")
    if changed:
        with pytest.raises(WorkspaceProblem, match="source support changed"):
            bench.add_answer_to_report(matter, ACTOR, report.report_id, conversation.conversation_id,
                message.message_id, expected_status=report.status)
        assert not bench.workspace.report_sections(matter.matter_id, report.report_id)
    else:
        section = bench.add_answer_to_report(matter, ACTOR, report.report_id, conversation.conversation_id,
            message.message_id, expected_status=report.status)
        refs = bench.workspace.report_citations(matter.matter_id, report.report_id, section.section_id)
        assert {reference.support_token for reference in refs} == {citation.support_token for citation in citations}
        assert RECOLLECTION.split(". ", 1)[1] in section.body
