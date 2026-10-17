"""Synthetic provenance, >12-unit recall, interruption and budget acceptance."""

from tests.fixture_write import fixture_write
from copy import deepcopy
import hashlib

import pytest

from case_intelligence.generation import GroundedGenerationService
from case_intelligence.hierarchical_synthesis import (
    POLICY, run_synthesis, synthesis_answer, validate_state,
)


class SourceEcho:
    available = True

    def __init__(self):
        self.calls = []

    def generate(self, **kwargs):
        self.calls.append(kwargs)
        return {"answerable": True, "claims": [
            {"text": source.excerpt, "evidence_ids": [source.evidence_id]}
            for source in kwargs["evidence"][:8]], "limitation": None, "missing_information": ""}


@pytest.fixture
def corpus():
    evidence, passes = [], []
    for index in range(24):
        text = (f"Synthetic dispatch record {index + 1} states the crate was delivered."
                if index != 23 else "Synthetic dispatch record 24 states the crate was not delivered.")
        token = hashlib.sha1(f"synthetic-{index}".encode()).hexdigest()
        evidence.append({"support_token": token, "source_name": f"synthetic-{index}.txt",
                         "location": "page 1", "excerpt": text, "evidence_kind": "document",
                         "document_id": f"synthetic-source-{index}", "source_version_id": "synthetic-v1"})
        passes.append({"answer": {"claims": [{"text": text, "citations": [{"support_token": token}]}]}})
    return passes, evidence


def execute(corpus, saved=None, checkpoint=lambda state: None, boundary=lambda: None, now=lambda: 1000):
    passes, evidence = corpus
    client = SourceEcho()
    state = run_synthesis("What do the dispatch records say about delivery?", passes, evidence,
                          GroundedGenerationService(client), checkpoint, boundary, saved, now=now)
    return state, client


def test_support_and_contradiction_across_24_units_keep_originals_through_both_levels(corpus):
    state, client = execute(corpus)
    validate_state(state, *corpus, final=True)
    answer = synthesis_answer(state, corpus[1])
    assert len(answer.claims) == 24
    assert any("was delivered" in claim.text for claim in answer.claims)
    assert any("was not delivered" in claim.text for claim in answer.claims)
    assert answer.claims[-1].evidence_ids == ("S24",)
    assert len(state["issue"]) == len(state["matter"]) == 6
    assert len({parent.split("C")[0] for parent in state["matter"][0]["parents"]}) > 1
    assert not state["partial"] and state["stop_reason"] == "completed"
    assert state["requests_spent"] == len(client.calls) == 12
    for call in client.calls:
        assert len(call["evidence"]) <= 12
        assert all(item.source_name.startswith("synthetic-") for item in call["evidence"])
        assert "derived_findings_not_evidence" in call["working_context"]


def test_interruption_reuses_nodes_but_does_not_refund_inflight_request(corpus):
    saved = []
    def checkpoint(state):
        saved.append(deepcopy(state))
        if state["requests_spent"] == 3 and len(state["issue"]) == 2:
            raise RuntimeError("Synthetic crash before response")
    with pytest.raises(RuntimeError, match="Synthetic crash"):
        execute(corpus, checkpoint=checkpoint)
    resumed, client = execute(corpus, saved[-1])
    assert resumed["issue"][:2] == saved[-1]["issue"]
    assert resumed["requests_spent"] == 13
    assert len(client.calls) == 10
    completed, client = execute(corpus, resumed)
    assert not client.calls and completed == resumed


def test_unsupported_saved_finding_is_never_promoted(corpus):
    corpus[0][0]["answer"]["claims"][0]["text"] = "A submarine transported 9999 satellites to Jupiter."
    state, client = execute(corpus)
    assert state["rejected_findings"] == ["P1C1"] and state["partial"]
    assert "Jupiter" not in synthesis_answer(state, corpus[1]).text
    assert all("Jupiter" not in call["working_context"] for call in client.calls)


@pytest.mark.parametrize("mutation", ["text", "token", "parent", "partial", "omitted", "final"])
def test_corrupted_intermediate_or_completion_receipt_is_rejected(corpus, mutation):
    state, _ = execute(corpus)
    claim = state["issue"][0]["claims"][0]
    if mutation == "text":
        claim["text"] = "A submarine transported 9999 satellites to Jupiter."
    elif mutation == "token":
        claim["support_tokens"] = ["not-an-original"]
    elif mutation == "parent":
        claim["parents"] = ["invented-node"]
    elif mutation == "partial":
        state["partial"] = True
    elif mutation == "omitted":
        state["omitted_groups"] = ["invented"]
    else:
        state["matter"][0]["claims"][0]["text"] = "The crate was transported in 9999."
    with pytest.raises(ValueError):
        validate_state(state, *corpus, final=True)


def test_changed_source_invalidates_nodes_without_refunding_calls(corpus):
    state, _ = execute(corpus)
    corpus[1][0]["source_version_id"] = "synthetic-v2"
    with pytest.raises(ValueError, match="no longer matches"):
        validate_state(state, *corpus)
    rebuilt, client = execute(corpus, state)
    assert rebuilt["requests_spent"] == 24 and len(client.calls) == 12
    assert rebuilt["basis"] != state["basis"]


@pytest.mark.parametrize("reason", ["time_budget", "generation_budget"])
def test_budget_exhaustion_retains_explicit_partial_and_omitted_groups(corpus, reason):
    saved = []
    def checkpoint(state):
        saved.append(deepcopy(state))
        if state["requests_spent"] == 1:
            raise RuntimeError("Synthetic interruption")
    with pytest.raises(RuntimeError):
        execute(corpus, checkpoint=checkpoint)
    if reason == "generation_budget":
        saved[-1]["requests_spent"] = POLICY["generation_requests"]
    state, client = execute(corpus, saved[-1], now=lambda: 2000 if reason == "time_budget" else 1000)
    assert not client.calls and state["partial"]
    assert state["stop_reason"] == reason
    assert "matter_pending_issue_completion" in state["omitted_groups"]
    validate_state(state, *corpus, final=True)


def test_live_workflow_backup_clean_restore_and_exports(tmp_path):
    import json
    import sqlite3
    from dataclasses import replace
    from fastapi.testclient import TestClient
    from case_intelligence.managed_storage import StoragePolicy
    from case_intelligence.workbench import create_workbench_app
    from case_intelligence.workspace_store import WorkspaceStore
    from case_intelligence.work_product_exports import ExportProblem, validate_research_basis

    app = create_workbench_app(tmp_path / "runtime", generator=SourceEcho(), auth_mode="test",
                               storage_policy=StoragePolicy(reserve_bytes=0))
    with TestClient(app) as client:
        bench = app.state.workbench
        bench.research.close()
        bench.research = None
        response = client.post("/matters", data={"name": "Synthetic hierarchical acceptance"}, follow_redirects=False)
        matter = bench.workspace.get_active_matter(response.headers["location"].split("/")[2])
        citations = []
        for index in range(24):
            text = (f"Synthetic dispatch record {index + 1} states the crate was delivered."
                    if index != 23 else "Synthetic dispatch record 24 states the crate was not delivered.")
            name = f"synthetic-{index}.txt"
            assert client.post(f"/matters/{matter.slug}/uploads", files=[("files", (name, text.encode(), "text/plain"))]).status_code == 200
            citations.append(next(item for item in bench.search(matter, str(index + 1) + " dispatch", limit=20)
                                  if item.source_name == name))
        searches = []
        def search(matter_arg, question, query, **kwargs):
            searches.append(query)
            kwargs["retrieval_boundary"]["source_fingerprint"] = bench.workspace.source_availability_fingerprint(matter.matter_id)
            ordinal = int(query[-1])
            return tuple(citations[ordinal * 8:(ordinal + 1) * 8])
        bench._answer_search = search
        job, _ = bench.workspace.queue_research_job(matter.matter_id, matter.owner_id,
            "What do the dispatch records say about delivery?", "Synthetic hierarchy", "research-request-" + "c" * 32)
        claimed = bench.workspace.claim_research_job("synthetic-worker")
        claimed = bench.workspace.set_research_plan(job.job_id,
            {"queries": ["synthetic group 0", "synthetic group 1", "synthetic group 2"], "synthesis_version": 1}, 5)
        original_checkpoint = bench.workspace.checkpoint_research_job
        def checkpoint(job_id, result):
            saved = original_checkpoint(job_id, result)
            hierarchy = result.get("hierarchical_synthesis")
            if hierarchy and len(hierarchy["issue"]) == 2:
                raise RuntimeError("Synthetic interruption after saved issue groups")
            return saved
        bench.workspace.checkpoint_research_job = checkpoint
        with pytest.raises(RuntimeError, match="Synthetic interruption"):
            bench._process_research_job(claimed, lambda: False)
        bench.workspace.checkpoint_research_job = original_checkpoint
        destination = tmp_path / "clean-restore.sqlite"
        with sqlite3.connect(destination) as output:
            bench.workspace.connection.backup(output)
        restored = WorkspaceStore(destination)
        assert restored.recover_running_research_jobs() == 1
        assert restored.connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert not restored.connection.execute("PRAGMA foreign_key_check").fetchall()
        original_workspace = bench.workspace
        bench.workspace = restored
        try:
            resumed = restored.claim_research_job("synthetic-restored-worker")
            result = bench._process_research_job(resumed, lambda: False)
            completed = bench._finish_research_job(resumed, result)
            assert len(searches) == 3
            assert len(result["answer"]["claims"]) == 24
            assert "was not delivered" in result["summary"]
            assert completed.result["hierarchical_synthesis"]["requests_spent"] == 12
            for format_name in ("json", "markdown", "docx"):
                artifact = bench.export_research_work_product(matter, completed, format_name)
                assert artifact.body
                if format_name == "json":
                    exported = json.loads(artifact.body)["investigation"]
                    assert len(exported["hierarchical_synthesis"]["matter"]) == 6
                elif format_name == "markdown":
                    assert b"synthetic-23.txt" in artifact.body and b"matter6" in artifact.body
            corrupted = deepcopy(result)
            corrupted["hierarchical_synthesis"]["matter"][0]["claims"][0]["support_tokens"] = ["not-original"]
            with pytest.raises(ExportProblem, match="provenance"):
                validate_research_basis(matter, replace(completed, result=corrupted))
        finally:
            bench.workspace = original_workspace
            restored.close()


def test_unknown_checkpoint_version_cannot_be_reset_by_changed_inputs(corpus):
    state, _ = execute(corpus)
    state["version"] = 999
    corpus[1][0]["source_version_id"] = "synthetic-v2"
    with pytest.raises(ValueError, match="Unknown"):
        execute(corpus, state)


def test_model_invented_intermediate_statement_is_filtered_before_matter_context(corpus):
    class InventingClient(SourceEcho):
        def generate(self, **kwargs):
            raw = super().generate(**kwargs)
            raw["claims"].append({"text": "A submarine transported 9999 satellites to Jupiter.", "evidence_ids": ["S1"]})
            return raw
    client = InventingClient()
    state = run_synthesis("What do the dispatch records say about delivery?", *corpus,
                          GroundedGenerationService(client), lambda value: None, lambda: None)
    assert state["partial"]
    assert "Jupiter" not in synthesis_answer(state, corpus[1]).text
    assert all("Jupiter" not in call["working_context"] for call in client.calls)
    assert sum(node["omitted_claims"] for node in state["issue"]) > 0
    assert "generated statements omitted: 12" in synthesis_answer(state, corpus[1]).text


def test_boundary_revocation_after_generation_prevents_saving_response(corpus):
    saved = []
    client = SourceEcho()
    def boundary():
        if client.calls:
            raise RuntimeError("Synthetic membership revoked")
    with pytest.raises(RuntimeError, match="membership revoked"):
        run_synthesis("What do the dispatch records say about delivery?", *corpus,
                      GroundedGenerationService(client), lambda value: saved.append(deepcopy(value)), boundary)
    assert saved[-1]["requests_spent"] == 1
    assert saved[-1]["issue"] == []


def test_final_transcript_orientation_retains_machine_transcription_caution(corpus):
    from case_intelligence.generation import MEDIA_TRANSCRIPT_NOTICE
    state, _ = execute(corpus)
    # Isolate the final assembler's modality notice; source verification at the
    # generator and persisted-node boundaries has separate attribution tests.
    corpus[1][23]["evidence_kind"] = "transcript"
    answer = synthesis_answer(state, corpus[1])
    assert answer.evidence_notice == MEDIA_TRANSCRIPT_NOTICE


def test_partial_notice_identifies_rejected_findings_and_omitted_model_claims(corpus):
    corpus[0][0]["answer"]["claims"].append({"text": "A submarine transported 9999 satellites to Jupiter.",
                                          "citations": [{"support_token": corpus[1][0]["support_token"]}]})
    state, _ = execute(corpus)
    text = synthesis_answer(state, corpus[1]).text
    assert "rejected saved findings: 1 (P1C2)" in text
    assert "generated statements omitted: 0" in text


def test_transcript_backed_investigation_renders_caution_and_portable_version_links(tmp_path, monkeypatch):
    import io
    import json
    import zipfile
    from dataclasses import replace
    from fastapi.testclient import TestClient
    from case_intelligence.generation import MEDIA_TRANSCRIPT_NOTICE
    from case_intelligence.managed_storage import StoragePolicy
    from case_intelligence.workbench import create_workbench_app
    from case_intelligence.work_product_exports import ExportProblem, validate_research_basis
    from tests.test_matter_media_workflow import ImmediateMediaProcessor, EvidenceEchoGenerator, _matter, _upload_and_wait, ACTOR

    # Isolate this output/provenance regression from Linux ffprobe placement.
    # The uploaded fixture and persisted transcript still use the real media workflow.
    from case_intelligence.pilot_uploads import _MediaProbe
    monkeypatch.setattr("case_intelligence.pilot_uploads._probe_media",
                        lambda source, media_type: _MediaProbe(10000, False, True))
    from case_intelligence.media_preflight import REVISION
    monkeypatch.setattr("case_intelligence.media_evidence.inspect_recording", lambda *args, **kwargs: {
        "revision": REVISION, "outcome": "ready", "complete": True, "checked_ms": 10000,
        "first_speech_ms": 0, "last_speech_ms": 10000, "quiet_ms": 0,
        "leading_quiet_ms": 0, "trailing_quiet_ms": 0, "language": "not_assessed", "quality": [],
    })
    app = create_workbench_app(tmp_path / "runtime", generator=EvidenceEchoGenerator(), auth_mode="test",
        media_processor=ImmediateMediaProcessor(), media_poll_seconds=.01, storage_policy=StoragePolicy(reserve_bytes=0))
    with TestClient(app) as client:
        slug = _matter(client, "Synthetic hierarchy transcript")
        bench = app.state.workbench
        bench.research.close()
        bench.research = None
        document, _ = _upload_and_wait(client, slug)
        matter = bench.matter(slug, ACTOR)
        job, _ = bench.workspace.queue_research_job(matter.matter_id, ACTOR,
            "What does the machine transcript say?", "Synthetic spoken findings", "research-request-" + "f" * 32)
        claimed = bench.workspace.claim_research_job("synthetic-transcript-worker")
        result = bench._process_research_job(claimed, lambda: False)
        completed = bench._finish_research_job(claimed, result)
        assert result["answer"]["evidence_notice"] == MEDIA_TRANSCRIPT_NOTICE
        assert any(item["evidence_kind"] == "transcript" for item in result["evidence"])
        page = client.get(f"/matters/{slug}/research?job={job.job_id}")
        assert page.status_code == 200 and MEDIA_TRANSCRIPT_NOTICE in page.text
        assert "rejected saved findings:" in page.text and "generated statements omitted:" in page.text
        for format_name in ("json", "markdown", "docx"):
            artifact = bench.export_research_work_product(matter, completed, format_name)
            if format_name == "docx":
                with zipfile.ZipFile(io.BytesIO(artifact.body)) as archive:
                    rendered = archive.read("word/document.xml").decode()
            else:
                rendered = artifact.body.decode()
            assert MEDIA_TRANSCRIPT_NOTICE in rendered
            if format_name == "json":
                exported = json.loads(artifact.body)["investigation"]
                ledger = {item["citation_id"]: item for item in exported["supporting_sources"]}
                for level in ("issue", "matter"):
                    for node in exported["hierarchical_synthesis"][level]:
                        for claim in node["claims"]:
                            for source in claim["sources"]:
                                assert source == ledger[source["citation_id"]]
                                assert source["version"] == document.version_id
                assert "support_token" not in rendered and "source_version_id" not in rendered
            else:
                assert "rejected saved findings:" in rendered and "generated statements omitted:" in rendered
        broken = deepcopy(result)
        broken["answer"].pop("evidence_notice")
        with pytest.raises(ExportProblem, match="provenance"):
            validate_research_basis(matter, replace(completed, result=broken))


from tests.test_investigation_planner import chain


def test_removing_cited_source_from_selected_set_during_synthesis_refuses_save(chain, monkeypatch):
    from case_intelligence.workflow_jobs import WorkflowFailure
    bench, client, matter, original_job, calls, citations = chain
    bench.workspace.cancel_research_job(matter.matter_id, matter.owner_id, original_job.job_id)
    selected = bench.workspace.create_source_set(matter.matter_id, "Synthetic scoped hierarchy",
        [item.document_id for item in citations.values()], matter.owner_id)
    job, _ = bench.workspace.queue_research_job(matter.matter_id, matter.owner_id,
        original_job.question, "Synthetic scoped hierarchy", "research-request-" + "d" * 32,
        source_set_id=selected.source_set_id)
    original_answer = bench.generator.answer
    def answer(*args, **kwargs):
        result = original_answer(*args, **kwargs)
        if kwargs.get("working_context"):
            bench.workspace.remove_source_organization(matter.matter_id, citations["first"].document_id)
        return result
    monkeypatch.setattr(bench.generator, "answer", answer)
    claimed = bench.workspace.claim_research_job("synthetic-scoped-worker")
    with pytest.raises(WorkflowFailure, match="left the selected set"):
        bench._process_research_job(claimed, lambda: False)
    saved = bench.workspace.research_job(matter.matter_id, matter.owner_id, job.job_id)
    assert saved.result["hierarchical_synthesis"]["requests_spent"] == 1
    assert saved.result["hierarchical_synthesis"]["issue"] == []


def test_failed_synthesis_displays_saved_issue_checkpoint_and_spent_budget(chain, monkeypatch):
    bench, client, matter, job, calls, citations = chain
    original_answer = bench.generator.answer
    synthesis_calls = []
    def answer(*args, **kwargs):
        if kwargs.get("working_context"):
            synthesis_calls.append(kwargs)
            if len(synthesis_calls) == 2:
                raise RuntimeError("Synthetic matter-stage interruption")
        return original_answer(*args, **kwargs)
    monkeypatch.setattr(bench.generator, "answer", answer)
    claimed = bench.workspace.claim_research_job("synthetic-failing-worker")
    with pytest.raises(RuntimeError, match="matter-stage interruption"):
        bench._process_research_job(claimed, lambda: False)
    bench.workspace.fail_research_job(job.job_id, "Synthetic matter-stage interruption")
    saved = bench.workspace.research_job(matter.matter_id, matter.owner_id, job.job_id)
    assert len(saved.result["hierarchical_synthesis"]["issue"]) == 1
    assert saved.result["hierarchical_synthesis"]["requests_spent"] == 2
    page = client.get(f"/matters/{matter.slug}/research?job={job.job_id}")
    assert page.status_code == 200
    assert "Issue and matter synthesis checkpoints" in page.text
    assert "Run state: failed. 2 of 32 generation requests charged" in page.text
    assert "Issue section 1" in page.text
    assert "Intermediate summaries are not evidence sources" in page.text
    corrupted = deepcopy(saved.result)
    corrupted["hierarchical_synthesis"]["issue"][0]["claims"][0]["text"] = "A submarine transported 9999 satellites to Jupiter."
    import json
    with fixture_write(bench.workspace):
        bench.workspace.connection.execute("UPDATE workbench_research_job SET result_json=? WHERE job_id=?",
            (json.dumps(corrupted), job.job_id))
    invalid = client.get(f"/matters/{matter.slug}/research?job={job.job_id}")
    assert invalid.status_code == 200 and "checkpoint could not be verified" in invalid.text
    assert "Jupiter" not in invalid.text


def test_source_set_removal_after_processing_refuses_final_transaction(chain):
    from case_intelligence.workspace_store import WorkspaceProblem
    bench, client, matter, original_job, calls, citations = chain
    bench.workspace.cancel_research_job(matter.matter_id, matter.owner_id, original_job.job_id)
    selected = bench.workspace.create_source_set(matter.matter_id, "Synthetic final scope",
        [item.document_id for item in citations.values()], matter.owner_id)
    job, _ = bench.workspace.queue_research_job(matter.matter_id, matter.owner_id,
        original_job.question, "Synthetic final scope", "research-request-" + "e" * 32,
        source_set_id=selected.source_set_id)
    claimed = bench.workspace.claim_research_job("synthetic-final-scope-worker")
    result = bench._process_research_job(claimed, lambda: False)
    assert result["hierarchical_synthesis"]["matter"]
    bench.workspace.remove_source_organization(matter.matter_id, citations["first"].document_id)
    with pytest.raises(WorkspaceProblem, match="left the selected set"):
        bench._finish_research_job(claimed, result)
    saved = bench.workspace.research_job(matter.matter_id, matter.owner_id, job.job_id)
    assert saved.state == "running"
    assert saved.result_message_id is None
    assert not bench.workspace.connection.in_transaction
