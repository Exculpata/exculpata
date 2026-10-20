"""Real synthetic rejection and exact historical display preserve saved evidence."""
from __future__ import annotations

from tests.fixture_write import fixture_write

import copy
from dataclasses import replace
import json
import zipfile
import io

import pytest

from case_intelligence.generation import GroundedGenerationService
from tests.test_generated_review_confidence import SyntheticGenerator
from tests.test_modality_confidence_presentation import readable
from tests.test_report_review_basis import ACTOR, convert, saved_research, workspace

LEGACY = ("I found potentially relevant source passages, but the generated "
    "answer did not pass source verification. Review the matches below "
    "or ask a narrower question.")
CURRENT = ("I found potentially relevant source passages, but no generated answer "
    "was retained after automated checks. Review the matches below or ask a narrower question.")


class RejectedGenerator(SyntheticGenerator):
    def __init__(self):
        super().__init__("A helicopter transported chemical samples to Denver.")
        self.calls = 0

    def generate(self, **kwargs):
        self.calls += 1
        return super().generate(**kwargs)


@pytest.mark.parametrize("workflow", ["answer", "durable", "research"])
def test_real_rejected_generation_records_neutral_abstention(workspace, monkeypatch, workflow):
    client, bench, matter = workspace
    _old_job, document = saved_research(bench, matter)
    candidate = bench._candidate(matter, document, document.parsed_units()[0], 1)
    citation = bench._citation(matter, candidate)
    monkeypatch.setattr(bench, "_answer_search", lambda *args, **kwargs: (citation,))
    generator = RejectedGenerator()
    bench.generator = GroundedGenerationService(generator)
    original = bench.source_store(matter).source_path(document.document_id).read_bytes()
    if workflow in {"answer", "durable"}:
        conversation = bench.workspace.get_conversation(matter.matter_id)
        if workflow == "durable":
            bench.answers.close()
            bench.workspace.queue_answer_job(matter.matter_id, conversation.conversation_id, ACTOR,
                "When did the bicycle arrive?", "answer-request-" + "e" * 32)
            job = bench.workspace.claim_answer_job("synthetic-rejection-worker")
            result = bench._process_answer_job(job, lambda *args: None, lambda: False)
            answer = bench._finish_answer_job(job, result)
        else:
            answer = bench.ask(matter, conversation, "When did the bicycle arrive?")
        assert answer.payload["kind"] == "not-supported"
        assert answer.payload["claims"] == []
        assert answer.payload["source_matches"][0]["support_token"] == citation.support_token
        assert answer.content == CURRENT
        assert answer.payload["missing_information"] == CURRENT
        for suffix in ("", "/assistant"):
            page = client.get(f"/matters/{matter.slug}{suffix}")
            assert page.status_code == 200 and CURRENT in page.text and LEGACY not in page.text
    else:
        bench.workspace.queue_research_job(matter.matter_id, ACTOR,
            "When did the bicycle arrive?", "Synthetic rejected generation", "research-request-" + "d" * 32)
        job = bench.workspace.claim_research_job("synthetic-rejection-worker")
        job = replace(job, plan={"queries": ["bicycle arrival"], "method": "One recorded search."})
        result = bench._process_research_job(job, lambda: False)
        assert result["answer"]["answerable"] is False
        assert result["summary"] == CURRENT
        assert result["passes"][0]["status"] == "needs_review"
        assert result["passes"][0]["text"] == CURRENT
        assert result["evidence"][0]["support_token"] == citation.support_token
    assert generator.calls >= 2  # Actual text checks and repair ran; no rejection exception stub.
    assert bench.source_store(matter).source_path(document.document_id).read_bytes() == original


@pytest.mark.parametrize("text", [LEGACY, " " + LEGACY, LEGACY + " Reviewer annotation."])
def test_historical_abstention_views_exports_reports_preserve_raw_message(workspace, text):
    client, bench, matter = workspace
    expected = CURRENT if text == LEGACY else text
    conversation = bench.workspace.get_conversation(matter.matter_id)
    message = bench.workspace.append_message(matter.matter_id, conversation.conversation_id,
        "assistant", text, {"kind": "not-supported", "answerable": False, "claims": [],
                            "missing_information": text, "source_matches": []})
    raw = bench.workspace.connection.execute("SELECT payload_json FROM workbench_message WHERE message_id=?",
        (message.message_id,)).fetchone()[0]
    for suffix in ("", "/assistant"):
        page = client.get(f"/matters/{matter.slug}{suffix}")
        assert page.status_code == 200 and expected in page.text
        if text == LEGACY:
            assert LEGACY not in page.text
    for suffix in ("/export", f"/messages/{message.message_id}/export"):
        for fmt in ("markdown", "docx"):
            artifact = client.get(f"/matters/{matter.slug}/conversations/{conversation.conversation_id}{suffix}?format={fmt}")
            assert artifact.status_code == 200
            rendered = readable(artifact.content, fmt)
            assert expected.strip() in rendered
            assert (CURRENT in rendered) is (text == LEGACY)
    bundle = client.get(f"/matters/{matter.slug}/export")
    assert bundle.status_code == 200
    with zipfile.ZipFile(io.BytesIO(bundle.content)) as archive:
        portable = json.loads(archive.read("conversations.json"))
        assert (CURRENT in json.dumps(portable)) is (text == LEGACY)
    report = bench.workspace.create_report(matter.matter_id, ACTOR, "Synthetic abstention copy")
    response = client.post(f"/matters/{matter.slug}/reports/{report.report_id}/from-answer/{conversation.conversation_id}/{message.message_id}",
        data={"expected_status": report.status}, follow_redirects=False)
    assert response.status_code == 303
    section, = bench.workspace.report_sections(matter.matter_id, report.report_id)
    assert section.body == expected.strip()
    assert bench.workspace.messages(matter.matter_id, conversation.conversation_id)[-1] == message
    assert bench.workspace.connection.execute("SELECT payload_json FROM workbench_message WHERE message_id=?",
        (message.message_id,)).fetchone()[0] == raw


def test_historical_rejected_research_projects_after_validation_preserving_ledger(workspace):
    client, bench, matter = workspace
    job, document = saved_research(bench, matter, answerable=False, potential_sources=True)
    result = copy.deepcopy(job.result)
    result["summary"] = result["answer"]["missing_information"] = LEGACY
    result["passes"][0]["text"] = result["passes"][0]["answer"]["missing_information"] = LEGACY
    result["gaps"][0]["note"] = LEGACY
    encoded = json.dumps(result)
    original = bench.source_store(matter).source_path(document.document_id).read_bytes()
    with fixture_write(bench.workspace):
        bench.workspace.connection.execute("UPDATE workbench_research_job SET result_json=? WHERE job_id=?", (encoded, job.job_id))
    job = bench.workspace.research_job(matter.matter_id, ACTOR, job.job_id)
    page = client.get(f"/matters/{matter.slug}/research?job={job.job_id}")
    assert page.status_code == 200 and CURRENT in page.text and LEGACY not in page.text
    for fmt in ("json", "markdown", "docx"):
        response = client.get(f"/matters/{matter.slug}/research/{job.job_id}/export?format={fmt}")
        assert response.status_code == 200
        rendered = readable(response.content, fmt)
        assert CURRENT in rendered and LEGACY not in rendered
    report = convert(client, bench, matter, job)
    rendered = "\n".join(section.body for section in bench.workspace.report_sections(matter.matter_id, report.report_id))
    assert CURRENT in rendered and LEGACY not in rendered
    assert bench.workspace.connection.execute("SELECT result_json FROM workbench_research_job WHERE job_id=?", (job.job_id,)).fetchone()[0] == encoded
    assert job.result["evidence"] == result["evidence"]
    assert bench.source_store(matter).source_path(document.document_id).read_bytes() == original
    # Presentation must never repair a raw saved-summary mismatch into a valid export.
    corrupted = copy.deepcopy(result)
    corrupted["summary"] = CURRENT
    with fixture_write(bench.workspace):
        bench.workspace.connection.execute("UPDATE workbench_research_job SET result_json=? WHERE job_id=?", (json.dumps(corrupted), job.job_id))
    assert client.get(f"/matters/{matter.slug}/research/{job.job_id}/export?format=json").status_code == 409


def test_real_malformed_generation_and_historical_failure_status_preserve_saved_event(workspace, monkeypatch):
    from case_intelligence.answer_jobs import AnswerJobFailure
    client, bench, matter = workspace
    _old, document = saved_research(bench, matter)
    citation = bench._citation(matter, bench._candidate(matter, document, document.parsed_units()[0], 1))
    bench.source_store(matter)
    monkeypatch.setattr(bench, "_answer_search", lambda *args, **kwargs: (citation,))
    class MalformedGenerator:
        available = True
        def generate(self, **kwargs):
            return {"synthetic_invalid_contract": True}
    bench.generator = GroundedGenerationService(MalformedGenerator())
    bench.answers.close()
    conversation = bench.workspace.get_conversation(matter.matter_id)
    bench.workspace.queue_answer_job(matter.matter_id, conversation.conversation_id, ACTOR,
        "When did the bicycle arrive?", "answer-request-" + "a" * 32)
    job = bench.workspace.claim_answer_job("synthetic-malformed-worker")
    expected = "No generated answer was retained after automated checks. Try a narrower question or search the matter."
    with pytest.raises(AnswerJobFailure, match="No generated answer was retained after automated checks"):
        bench._process_answer_job(job, lambda *args: None, lambda: False)
    historical = "I could not verify enough source support for a reliable answer. Try a narrower question or search the matter."
    saved = bench.workspace.fail_answer_job(job.job_id, historical)
    events = bench.workspace.answer_events(matter.matter_id, ACTOR, job.job_id)
    response = client.get(f"/matters/{matter.slug}/answer-jobs/{job.job_id}")
    assert response.status_code == 200
    assert response.json()["message"] == expected
    assert response.json()["events"][-1]["message"] == expected
    assert bench.workspace.get_answer_job(matter.matter_id, ACTOR, job.job_id) == saved
    assert bench.workspace.answer_events(matter.matter_id, ACTOR, job.job_id) == events


def test_real_classification_rejection_and_historical_machine_fields_leave_human_note(workspace, monkeypatch):
    client, bench, matter = workspace
    bench.full_review.close()
    _old, document = saved_research(bench, matter)
    bench.source_store(matter)
    criterion, version = bench.workspace.create_review_criterion(matter.matter_id, ACTOR,
        title="Synthetic screening", instructions="Include bicycle records.")
    run = bench.workspace.queue_review_run(matter.matter_id, ACTOR, version.criterion_version_id, run_kind="full")
    bench.workspace.claim_review_run("synthetic-screening-worker")
    citation = bench._citation(matter, bench._candidate(matter, document, document.parsed_units()[0], 1))
    monkeypatch.setattr(bench, "search", lambda *args, **kwargs: (citation,))
    bench.generator = GroundedGenerationService(RejectedGenerator())
    decision = bench.workspace.review_decision(matter.matter_id, ACTOR, run.run_id, document.document_id)
    result = bench._process_review_decision(run, decision, lambda: False)
    current = "Potentially relevant passages were found, but no inclusion decision was retained after automated checks."
    current_error = "Automated checks did not retain an inclusion decision. Review the source directly."
    historical = "Potentially relevant passages were found, but an inclusion decision did not pass source verification."
    historical_error = "Source verification did not resolve an inclusion decision."
    assert result.decision == "needs_attention" and result.rationale == current
    assert result.error_message == current_error
    assert result.citations[0]["support_token"] == citation.support_token
    bench.workspace.record_review_decision(run.run_id, document.document_id, decision="needs_attention",
        rationale=historical, error_message=historical_error, citations=result.citations)
    bench.workspace.finish_review_run(run.run_id)
    saved = bench.workspace.review_decision(matter.matter_id, ACTOR, run.run_id, document.document_id)
    bench.workspace.adjudicate_review_decision(matter.matter_id, ACTOR, run.run_id, document.document_id,
        human_decision="exclude", expected_updated_at=saved.updated_at, note=historical_error)
    saved = bench.workspace.review_decision(matter.matter_id, ACTOR, run.run_id, document.document_id)
    page = client.get(f"/matters/{matter.slug}/full-review", params={"criterion": criterion.criterion_id, "run": run.run_id, "source": document.document_id})
    assert page.status_code == 200 and current in page.text and current_error in page.text
    for fmt in ("json", "csv", "markdown", "docx"):
        response = client.get(f"/matters/{matter.slug}/full-review/{run.run_id}/export?format={fmt}")
        assert response.status_code == 200
        text = readable(response.content, fmt)
        assert current in text and historical not in text
        assert historical_error in text  # Exact reviewer-authored words must remain.
        if fmt in {"json", "csv"}:
            assert current_error in text
    response = client.post(f"/matters/{matter.slug}/full-review/{run.run_id}/report", follow_redirects=False)
    assert response.status_code == 303
    report = bench.workspace.reports(matter.matter_id, ACTOR)[0]
    text = "\n".join(section.body for section in bench.workspace.report_sections(matter.matter_id, report.report_id))
    assert current in text and historical not in text
    assert current_error in text and historical_error in text
    assert bench.workspace.review_decision(matter.matter_id, ACTOR, run.run_id, document.document_id) == saved


def test_legacy_research_pass_without_answer_projects_only_after_raw_validation(workspace):
    client, bench, matter = workspace
    job, _document = saved_research(bench, matter, answerable=False, potential_sources=True)
    historical = "Potential passages were found, but this research step did not produce a source-verified finding."
    expected = "Potential passages were found, but no generated finding was retained for this research step after automated checks."
    result = copy.deepcopy(job.result)
    result["passes"][0].pop("answer")
    result["passes"][0].update(status="needs_review", text=historical)
    result["gaps"][0]["note"] = historical
    encoded = json.dumps(result)
    with fixture_write(bench.workspace):
        bench.workspace.connection.execute("UPDATE workbench_research_job SET result_json=? WHERE job_id=?", (encoded, job.job_id))
    saved = bench.workspace.research_job(matter.matter_id, ACTOR, job.job_id)
    page = client.get(f"/matters/{matter.slug}/research?job={job.job_id}")
    assert page.status_code == 200 and expected in page.text and historical not in page.text
    for fmt in ("json", "markdown", "docx"):
        response = client.get(f"/matters/{matter.slug}/research/{job.job_id}/export?format={fmt}")
        assert response.status_code == 200
        text = readable(response.content, fmt)
        assert expected in text and historical not in text
    report = convert(client, bench, matter, saved)
    text = "\n".join(section.body for section in bench.workspace.report_sections(matter.matter_id, report.report_id))
    assert expected in text and historical not in text
    assert "no generated finding citing them was retained" in text
    assert bench.workspace.connection.execute("SELECT result_json FROM workbench_research_job WHERE job_id=?", (job.job_id,)).fetchone()[0] == encoded
    corrupted = copy.deepcopy(result)
    corrupted["passes"][0]["text"] = expected
    with fixture_write(bench.workspace):
        bench.workspace.connection.execute("UPDATE workbench_research_job SET result_json=? WHERE job_id=?", (json.dumps(corrupted), job.job_id))
    assert client.get(f"/matters/{matter.slug}/research/{job.job_id}/export?format=json").status_code == 409


def test_exact_historical_words_in_manual_message_stay_authored_text(workspace):
    client, bench, matter = workspace
    conversation = bench.workspace.get_conversation(matter.matter_id)
    message = bench.workspace.append_message(matter.matter_id, conversation.conversation_id, "assistant",
        LEGACY, {"kind": "manual", "missing_information": LEGACY})
    for suffix in ("", "/assistant"):
        response = client.get(f"/matters/{matter.slug}{suffix}")
        assert response.status_code == 200 and LEGACY in response.text and CURRENT not in response.text
    response = client.get(f"/matters/{matter.slug}/conversations/{conversation.conversation_id}/export?format=markdown")
    assert response.status_code == 200 and LEGACY in response.text and CURRENT not in response.text
    assert bench.workspace.messages(matter.matter_id, conversation.conversation_id)[-1] == message
