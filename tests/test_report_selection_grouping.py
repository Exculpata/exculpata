"""Saved-work grouping survives expansion, durable jobs and offline compilation."""
import pytest

from case_intelligence.report_compilation import compilation_fingerprint
from case_intelligence.report_materials import snapshot_report_materials
from tests.test_guided_reports import finished, queue
from tests.test_report_review_basis import ACTOR, saved_research, workspace  # noqa: F401 - pytest fixture registration


@pytest.mark.parametrize("kind", ["research", "conversation", "review"])
def test_one_offline_saved_work_selection_can_expand_into_many_report_materials(workspace, kind):
    client, bench, matter = workspace
    research, document = saved_research(bench, matter)
    if kind == "research":
        selected = f"research:{research.job_id}"
    elif kind == "review":
        bench.full_review.close()
        bench.source_store(matter)
        _criterion, version = bench.workspace.create_review_criterion(matter.matter_id, ACTOR,
            title="Synthetic grouped source check", instructions="Include bicycle records.")
        run = bench.workspace.queue_review_run(matter.matter_id, ACTOR, version.criterion_version_id, run_kind="full")
        bench.workspace.claim_review_run("synthetic-group-worker")
        candidate = bench._candidate(matter, document, document.parsed_units()[0], 1)
        citation = bench._workflow_citation_payload(bench._citation(matter, candidate))
        bench.workspace.record_review_decision(run.run_id, document.document_id, decision="included",
            rationale="The source describes a bicycle.", citations=[citation])
        bench.workspace.finish_review_run(run.run_id)
        selected = f"review:{run.run_id}"
    else:
        conversation = bench.workspace.create_conversation(matter.matter_id, "Synthetic grouped conversation", actor_id=ACTOR)
        bench.workspace.append_message(matter.matter_id, conversation.conversation_id, "user", "Review the bicycle delivery.")
        bench.workspace.append_message(matter.matter_id, conversation.conversation_id, "assistant",
                                       "The bicycle arrival was saved.", payload=research.result["answer"])
        selected = f"conversation:{conversation.conversation_id}"
    with bench.source_store(matter).mutation_guard(), bench.workspace._lock:
        materials = snapshot_report_materials(bench, matter, ACTOR, (selected,))
    assert len(materials) > 1
    assert not bench.generator.available
    job_id = queue(client, matter, [selected], kind="topic", topic="bicycle delivery", key=f"synthetic-offline-{kind}")
    result = finished(client, matter, job_id)
    assert result["state"] == "succeeded", result
    job = bench.report_compilation.jobs.get(matter.matter_id, ACTOR, job_id)
    assert job.selections == (selected,)
    assert job.input_fingerprint == compilation_fingerprint("topic", "bicycle delivery", materials,
                                                          budget=bench.report_compilation.budget, selections=job.selections)
    sections = bench.workspace.report_sections(matter.matter_id, job.report_id)
    assert "topic relevance has not been checked" in sections[-1].prose
    assert "Selected saved work: 1." in sections[-1].compilation_basis
    assert all("relevance not checked" in section.heading for section in sections[:-1])
    assert client.get(f"/matters/{matter.slug}/reports/{job.report_id}/export?format=markdown").status_code == 200


def test_multiple_offline_saved_work_selections_still_require_relevance_check(workspace):
    client, bench, matter = workspace
    research, _ = saved_research(bench, matter)
    conversation = bench.workspace.create_conversation(matter.matter_id, "Synthetic second selection", actor_id=ACTOR)
    bench.workspace.append_message(matter.matter_id, conversation.conversation_id, "user", "Compare another synthetic topic.")
    job_id = queue(client, matter, [f"research:{research.job_id}", f"conversation:{conversation.conversation_id}"],
                   kind="topic", topic="bicycle delivery", key="synthetic-offline-multiple")
    result = finished(client, matter, job_id)
    assert result["state"] == "failed" and "Topic relevance could not be checked" in result["message"]
    assert not bench.workspace.reports(matter.matter_id, ACTOR)
