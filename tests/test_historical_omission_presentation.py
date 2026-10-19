"""Known service omissions are presentation-only; sourced qualifications stay exact."""
from __future__ import annotations

from tests.fixture_write import fixture_write

import copy
import io
import json
import zipfile

import pytest

from case_intelligence.answer_presentation import answer_content, answer_limitation
from case_intelligence.generation import LEGACY_VERIFICATION_OMISSION_NOTICE as OLD, VERIFICATION_OMISSION_NOTICE as NEW
from tests.test_modality_confidence_presentation import readable
from tests.test_report_review_basis import ACTOR, convert, saved_research, workspace


@pytest.mark.parametrize("explicit", [False, True])
@pytest.mark.parametrize("sourced", [False, True])
def test_historical_omission_views_exports_report_copies_leave_raw_evidence(workspace, explicit, sourced):
    client, bench, matter = workspace
    job, document = saved_research(bench, matter)
    result = copy.deepcopy(job.result)
    payload = result["answer"]
    payload.update(kind="generated", introduction="Generated answer for source review:", missing_information="", omitted_claims=1)
    citation = payload["claims"][0]["citations"][0]
    qualification = {"text": "The gate log was unavailable.", "citations": [citation]} if sourced else None
    old_limitation = (qualification["text"] + " " if qualification else "") + OLD
    new_limitation = (qualification["text"] + " " if qualification else "") + NEW
    payload["limitation"] = {"text": old_limitation, "citations": [citation] if sourced else []}
    if explicit:
        payload.update(source_limitation=qualification, verification_notice=OLD)
    content = payload["introduction"] + "\n" + payload["claims"][0]["text"] + "\nLimitation: " + old_limitation
    result["summary"] = content
    result["passes"][0].update(answer=payload, text=content)
    encoded = json.dumps(result)
    with fixture_write(bench.workspace):
        bench.workspace.connection.execute("UPDATE workbench_research_job SET result_json=? WHERE job_id=?", (encoded, job.job_id))
    job = bench.workspace.research_job(matter.matter_id, ACTOR, job.job_id)
    conversation = bench.workspace.get_conversation(matter.matter_id)
    message = bench.workspace.append_message(matter.matter_id, conversation.conversation_id, "assistant", content, payload)
    raw = bench.workspace.connection.execute("SELECT payload_json FROM workbench_message WHERE message_id=?", (message.message_id,)).fetchone()[0]
    original = bench.source_store(matter).source_path(document.document_id).read_bytes()
    for suffix in ("", "/assistant", f"/research?job={job.job_id}"):
        page = client.get(f"/matters/{matter.slug}{suffix}")
        assert page.status_code == 200 and new_limitation in page.text and OLD not in page.text
    for path, formats in (
        (f"/conversations/{conversation.conversation_id}/export", ("markdown", "docx")),
        (f"/conversations/{conversation.conversation_id}/messages/{message.message_id}/export", ("markdown", "docx")),
        (f"/research/{job.job_id}/export", ("json", "markdown", "docx")),
    ):
        for fmt in formats:
            response = client.get(f"/matters/{matter.slug}{path}?format={fmt}")
            assert response.status_code == 200
            text = readable(response.content, fmt)
            assert new_limitation in text and OLD not in text
    bundle = client.get(f"/matters/{matter.slug}/export")
    assert bundle.status_code == 200
    with zipfile.ZipFile(io.BytesIO(bundle.content)) as archive:
        portable = json.loads(archive.read("conversations.json"))
        assert new_limitation in json.dumps(portable) and OLD not in json.dumps(portable)
    report = convert(client, bench, matter, job)
    text = "\n".join(section.body for section in bench.workspace.report_sections(matter.matter_id, report.report_id))
    assert new_limitation in text and OLD not in text
    report = bench.workspace.create_report(matter.matter_id, ACTOR, "Synthetic omission copy")
    response = client.post(f"/matters/{matter.slug}/reports/{report.report_id}/from-answer/{conversation.conversation_id}/{message.message_id}",
        data={"expected_status": report.status}, follow_redirects=False)
    assert response.status_code == 303
    section, = bench.workspace.report_sections(matter.matter_id, report.report_id)
    assert new_limitation in section.body and OLD not in section.body
    assert bench.workspace.connection.execute("SELECT payload_json FROM workbench_message WHERE message_id=?", (message.message_id,)).fetchone()[0] == raw
    assert bench.workspace.connection.execute("SELECT result_json FROM workbench_research_job WHERE job_id=?", (job.job_id,)).fetchone()[0] == encoded
    assert bench.workspace.messages(matter.matter_id, conversation.conversation_id)[-1] == message
    assert bench.source_store(matter).source_path(document.document_id).read_bytes() == original
    assert job.result["evidence"] == result["evidence"]


@pytest.mark.parametrize("payload", [
    {"limitation": {"text": OLD}, "omitted_claims": 0},
    {"limitation": {"text": OLD}, "omitted_claims": True},
    {"limitation": {"text": " " + OLD}, "omitted_claims": 1},
    {"limitation": {"text": " Source qualification. " + OLD}, "omitted_claims": 1},
    {"limitation": {"text": OLD + " Reviewer note."}, "omitted_claims": 1},
    {"limitation": {"text": OLD}, "source_limitation": {"text": OLD}, "verification_notice": OLD, "omitted_claims": 1},
    {"limitation": {"text": " " + OLD}, "source_limitation": None, "verification_notice": OLD, "omitted_claims": 1},
])
def test_omission_projection_preserves_unrecognized_or_sourced_text(payload):
    text = payload["limitation"]["text"]
    assert answer_limitation(payload) == text
    content = "Synthetic review\nLimitation: " + text
    assert answer_content(content, "Synthetic review", payload) == content


def test_omission_projection_preserves_source_quote_and_nonmatching_content():
    # The same words inside a recorded source qualification are never rewritten.
    source = 'The source quotes "' + OLD + '".'
    payload = {"limitation": {"text": source + " " + OLD},
               "source_limitation": {"text": source}, "verification_notice": OLD}
    assert answer_limitation(payload) == source + " " + NEW
    quoted = 'Reviewer quotes "' + payload["limitation"]["text"] + '".'
    assert answer_content(quoted, "", payload) == quoted
