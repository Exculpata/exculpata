"""Synthetic evidence for explicit review budgets and durable accounting."""
import html
import io
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from case_intelligence.managed_storage import StoragePolicy
from case_intelligence.review_budget import ReviewBudget, budget_metadata
from case_intelligence.review_bench_v2 import (
    Candidate, DeterministicEmbeddingAdapter, DeterministicReranker,
    HybridRetriever, InMemoryHybridBackend,
)
from case_intelligence.workbench import create_workbench_app
from case_intelligence.workspace_store import WorkspaceStore
ACTOR = "synthetic-budget-reviewer"


class EvidenceEchoGenerator:
    available = True

    def generate(self, **kwargs):
        evidence = kwargs["evidence"]
        return {"answerable": bool(evidence), "claims": [
            {"text": item.excerpt[:600], "evidence_ids": [item.evidence_id]}
            for item in evidence[:1]
        ], "limitation": None, "missing_information": ""}


def _store(path):
    store = WorkspaceStore(path / "workbench.sqlite")
    principal = store.upsert_principal("test", ACTOR, "Synthetic reviewer", ACTOR, preferred_principal_id=ACTOR)
    matter = store.create_matter("Generated budget matter", "Synthetic", principal.principal_id)
    return store, matter


@pytest.mark.parametrize("available", [3, 50])
def test_primary_request_is_validated_before_search_not_silently_capped(available):
    backend = InMemoryHybridBackend([
        Candidate("synthetic", f"doc-{i}", f"chunk-{i}", "Generated source.txt", 1,
                  "Generated device reached ready state", 1.0)
        for i in range(available)
    ])
    retriever = HybridRetriever(backend, DeterministicEmbeddingAdapter(), DeterministicReranker())
    with pytest.raises(ValueError, match="effective budget"):
        retriever.search("synthetic", "generated device", limit=30)
    assert backend.calls == []
    assert len(retriever.search("synthetic", "generated device", limit=20)) == min(available, 20)


@pytest.mark.parametrize("fields", [{"passes": 16}, {"primary_candidates": 30}, {"passes": True}, {"evidence_chars": 0}])
def test_invalid_budget_rejected(fields):
    with pytest.raises(ValueError, match="Review budget"):
        ReviewBudget(**fields)


def test_repeated_passages_budget_agrees_with_ui_and_exports(tmp_path, monkeypatch):
    # This synthetic upload exercises review accounting, independently of an
    # operator's capacity reserve. Production still uses its configured policy.
    monkeypatch.setenv("CASE_INTELLIGENCE_STORAGE_RESERVE_GIB", "16384")
    app = create_workbench_app(tmp_path / "runtime", generator=EvidenceEchoGenerator(), auth_mode="test",
        storage_policy=StoragePolicy(reserve_bytes=0))
    with TestClient(app) as client:
        bench = app.state.workbench
        assert bench.storage_policy.reserve_bytes == 0
        bench.research.close()
        bench.research = None
        created = client.post("/matters", data={"name": "Generated budget matter", "descriptor": "Synthetic"}, follow_redirects=False)
        slug = created.headers["location"].split("/")[2]
        uploaded = client.post(f"/matters/{slug}/uploads", files=[("files", ("generated.txt", b"Generated device G-24 entered ready state at 09:14.", "text/plain"))], follow_redirects=False)
        assert uploaded.status_code == 303
        matter = bench.workspace.get_active_matter(slug)
        queued, _ = bench.workspace.queue_research_job(matter.matter_id, matter.owner_id, "When did device G-24 enter ready state?", "Generated budget", "research-request-" + "a" * 32)
        claimed = bench.workspace.claim_research_job("synthetic-worker")
        result = bench._process_research_job(claimed, lambda: False)
        job = bench._finish_research_job(claimed, result)
        budget = job.review_budget
        assert budget["requested"] == budget["effective"]
        assert budget["counts"]["completed_passes"] == 3
        assert budget["counts"]["candidate_occurrences"] == 3
        assert budget["counts"]["unique_evidence"] == 1
        assert budget["counts"]["candidate_sources"] == 1
        assert budget["counts"]["analyzed_unit_occurrences"] == 1
        assert budget["counts"]["synthesis_inputs"] == 1
        assert budget["stop_reason"] == "no_new_evidence"
        page = client.get(f"/matters/{slug}/research?job={job.job_id}")
        assert html.escape(job.review_budget_description) in page.text
        status = client.get(f"/matters/{slug}/research/{job.job_id}/status").json()
        assert status["review_budget"] == budget
        exported = bench.export_research_work_product(matter, job, "json")
        payload = json.loads(exported.body)["investigation"]
        assert payload["review_budget"] == budget
        assert payload["review_budget_description"] == job.review_budget_description
        markdown = bench.export_research_work_product(matter, job, "markdown")
        assert job.review_budget_description in markdown.body.decode().replace("\\_", "_")


def test_budget_checkpoint_and_terminal_reason_survive_clean_backup_restore(tmp_path):
    store, matter = _store(tmp_path / "original")
    job, _ = store.queue_research_job(matter.matter_id, ACTOR, "Generated question", "Generated title", "research-request-" + "f" * 32)
    store.claim_research_job("synthetic-worker")
    budget = ReviewBudget().metadata(completed_passes=1, unique_evidence=1)
    store.set_research_plan(job.job_id, {"queries": ["Generated question"], "budget": budget}, 3)
    store.checkpoint_research_job(job.job_id, {"budget": budget, "passes": [{"query": "Generated question"}], "evidence": []})
    destination = tmp_path / "restored.sqlite"
    connection = sqlite3.connect(destination)
    with store._lock:
        store.connection.backup(connection)
    connection.close()
    store.close()
    restored = WorkspaceStore(destination)
    assert restored.recover_running_research_jobs() == 1
    recovered = restored.research_job(matter.matter_id, ACTOR, job.job_id)
    assert recovered.review_budget == budget
    restored.claim_research_job("synthetic-resumed-worker")
    restored.cancel_research_job(matter.matter_id, ACTOR, job.job_id)
    stopped = restored.fail_research_job(job.job_id, "Research cancelled.")
    assert stopped.review_budget["stop_reason"] == "cancelled"
    assert stopped.review_budget["counts"] == budget["counts"]
    restored.close()
    reopened = WorkspaceStore(destination)
    assert reopened.research_job(matter.matter_id, ACTOR, job.job_id).result["stop_reason"] == "cancelled"
    assert reopened.connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    reopened.close()


def test_legacy_metadata_remains_unknown_and_queued_cancel_is_durable(tmp_path):
    assert budget_metadata({"coverage": {"search_pass_count": 5}})["counts"] is None
    store, matter = _store(tmp_path)
    queued, _ = store.queue_research_job(matter.matter_id, ACTOR, "Generated question", "Generated title", "research-request-" + "e" * 32)
    stopped = store.cancel_research_job(matter.matter_id, ACTOR, queued.job_id)
    assert stopped.review_budget["status"] == "unknown"
    assert stopped.review_budget["stop_reason"] == "cancelled_before_start"
    store.close()


def test_long_packet_fits_generator_contract_and_accounts_for_omitted_text():
    from case_intelligence.generation import EvidenceItem, GroundedGenerationService

    budget = ReviewBudget()
    excerpts, omitted = budget.bound_excerpts(["Generated " * 700] * 12)
    packet = tuple(EvidenceItem(f"S{i}", "Generated source.txt", "Page 1", excerpt)
                   for i, excerpt in enumerate(excerpts, 1) if excerpt)
    assert sum(len(item.excerpt) for item in packet) == 48_000
    assert omitted == 84_000 - 48_000
    assert len(packet) == 8
    answer = GroundedGenerationService(EvidenceEchoGenerator()).answer("What was generated?", packet)
    assert answer.answerable


def test_export_budget_metadata_drops_unrecognized_saved_content():
    value = ReviewBudget().metadata(completed_passes=1)
    value["extra"] = "Synthetic field excluded from portable output"
    value["counts"]["unexpected"] = "Synthetic non-counter"
    portable = budget_metadata({"budget": value})
    assert "extra" not in portable
    assert "unexpected" not in portable["counts"]


def test_cancelled_checkpoint_crash_recovery_persists_terminal_budget_reason(tmp_path):
    store, matter = _store(tmp_path)
    job, _ = store.queue_research_job(matter.matter_id, ACTOR, "Generated question", "Generated title", "research-request-" + "d" * 32)
    store.claim_research_job("synthetic-worker")
    budget = ReviewBudget().metadata(completed_passes=1, unique_evidence=1)
    store.checkpoint_research_job(job.job_id, {"budget": budget, "passes": [{"query": "Generated question"}], "evidence": []})
    store.cancel_research_job(matter.matter_id, ACTOR, job.job_id)
    path = store.path
    store.close()  # Process exited before the worker acknowledged cancellation.
    reopened = WorkspaceStore(path)
    assert reopened.recover_running_research_jobs() == 1
    recovered = reopened.research_job(matter.matter_id, ACTOR, job.job_id)
    assert recovered.state == "cancelled"
    assert recovered.result["stop_reason"] == "cancelled"
    assert recovered.review_budget["stop_reason"] == "cancelled"
    assert recovered.review_budget["counts"] == budget["counts"]
    assert "Stop reason: cancelled." in recovered.review_budget_description
    reopened.close()


def test_ollama_generation_request_applies_the_shared_output_budget(monkeypatch):
    from case_intelligence import generation

    requests = []

    def request(url, payload, **kwargs):
        requests.append(payload)
        return {"message": {"content": json.dumps({"answerable": False, "claims": [], "limitation": None, "missing_information": "Synthetic unsupported question."})}}

    monkeypatch.setattr(generation, "_bounded_json_request", request)
    client = generation.OllamaGenerator("http://127.0.0.1:11434", "synthetic-generator")
    client.generate(question="What does the generated record say?", evidence=(generation.EvidenceItem("S1", "Generated source.txt", "Line 1", "Generated device entered ready state."),))
    assert len(requests) == 1
    assert requests[0]["options"]["num_predict"] == ReviewBudget().output_tokens == 1_200


def test_legacy_budget_does_not_invent_a_historical_time_limit():
    from case_intelligence.review_budget import budget_description
    value = ReviewBudget().metadata(completed_passes=2)
    value['version'] = 1
    value['requested'].pop('search_seconds')
    value['effective'].pop('search_seconds')
    saved = budget_metadata({'budget': value})
    assert saved['version'] == 1
    assert 'search_seconds' not in saved['effective']
    assert 'search-time budget not recorded' in budget_description(saved)
