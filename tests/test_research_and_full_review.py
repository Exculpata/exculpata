from __future__ import annotations

from tests.fixture_write import fixture_write


from case_intelligence.legacy_names import (
    LEGACY_PRODUCT,
)
import hashlib
import io
import json
import re
import sqlite3
import threading
import time
from dataclasses import asdict
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from case_intelligence.workflow_jobs import (
    ReviewCoordinator,
    ReviewDecisionResult,
    WorkflowFailure,
)
from case_intelligence.workbench import create_workbench_app
from case_intelligence.workspace_store import WorkspaceProblem, WorkspaceStore


ACTOR = "development-workflow-user"


class EvidenceEchoGenerator:
    available = True

    def generate(self, **kwargs):
        evidence = kwargs["evidence"]
        return {
            "answerable": bool(evidence),
            "claims": [
                {"text": item.excerpt[:600], "evidence_ids": [item.evidence_id]}
                for item in evidence[:1]
            ],
            "limitation": None,
            "missing_information": "",
        }


def _store(tmp_path: Path, count: int = 4):
    store = WorkspaceStore(tmp_path / "workbench.sqlite")
    principal = store.upsert_principal(
        "test", ACTOR, "Workflow Reviewer", ACTOR, preferred_principal_id=ACTOR
    )
    matter = store.create_matter("Generated workflow matter", "Public fixture", principal.principal_id)
    catalog = [
            {
                "document_id": f"{index:032x}",
                "version_id": f"{index + 10_000:032x}",
                "action_token": f"{index + 20_000:032x}",
                "display_name": f"Generated source {index:05d}.txt",
                "relative_path": f"generated/{index:05d}.txt",
                "media_type": "text/plain",
                "kind": "TXT",
                "source_state": "ready",
                "tone": "ready",
                "state_label": "Searchable",
                "count_label": "1 line",
                "processing_stage": "",
                "completed_units": 1,
                "total_units": 1,
                "page_count": 1,
                "duration_ms": 0,
                "byte_size": 40,
                "origin": "upload",
                "retryable": False,
                "removable": True,
                "has_video": False,
                "content_basis_digest": hashlib.sha256(
                    f"Generated source content {index}".encode("utf-8")
                ).hexdigest(),
            }
            for index in range(1, count + 1)
        ]
    store.upsert_source_catalog(
        matter.matter_id,
        catalog,
    )
    store.reconcile_source_organizations(
        matter.matter_id,
        tuple(
            (str(item["document_id"]), "upload", str(item["relative_path"]))
            for item in catalog
        ),
    )
    return store, matter


def test_workflow_migration_matches_operator_copy():
    root = Path(__file__).parents[1]
    assert (
        root / "src/case_intelligence/migrations/sqlite/0021_research_and_full_review.sql"
    ).read_bytes() == (
        root / "migrations/sqlite/0021_research_and_full_review.sql"
    ).read_bytes()
    assert (
        root / "src/case_intelligence/migrations/sqlite/0024_review_source_content_basis.sql"
    ).read_bytes() == (
        root / "migrations/sqlite/0024_review_source_content_basis.sql"
    ).read_bytes()


def test_source_check_basis_survives_online_backup_and_clean_restore(tmp_path):
    store, matter = _store(tmp_path / "origin", count=1)
    _criterion, version = store.create_review_criterion(
        matter.matter_id,
        ACTOR,
        title="Generated restore criterion",
        instructions="Include the generated source.",
    )
    run = store.queue_review_run(
        matter.matter_id, ACTOR, version.criterion_version_id, run_kind="full"
    )
    frozen = store.review_decisions_for_export(
        matter.matter_id, ACTOR, run.run_id
    )[0]
    assert len(frozen.source_basis_digest) == 64

    restored_path = tmp_path / "restored" / "workbench.sqlite"
    restored_path.parent.mkdir(parents=True)
    # fixture-write: Open a new isolated backup destination before its first WorkspaceStore.
    destination = sqlite3.connect(restored_path)
    try:
        with store._lock:
            # fixture-write: Copy under the source lock to a new isolated backup destination before its first WorkspaceStore.
            store.connection.backup(destination)
    finally:
        destination.close()  # fixture-write: Close the isolated backup destination before its first WorkspaceStore is opened.
        store.close()

    restored = WorkspaceStore(restored_path)
    try:
        columns = {
            str(row[1])
            for row in restored.connection.execute(
                "PRAGMA table_info(workbench_review_decision)"
            )
        }
        durable = restored.review_decisions_for_export(
            matter.matter_id, ACTOR, run.run_id
        )[0]
        assert "source_basis_digest" in columns
        assert durable.source_basis_digest == frozen.source_basis_digest
        assert restored.connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        restored.close()


def test_pre_basis_active_source_check_fails_closed_after_backup_restore(tmp_path):
    store, matter = _store(tmp_path / "legacy", count=1)
    _criterion, version = store.create_review_criterion(
        matter.matter_id,
        ACTOR,
        title="Generated legacy criterion",
        instructions="Include the generated source.",
    )
    run = store.queue_review_run(
        matter.matter_id, ACTOR, version.criterion_version_id, run_kind="full"
    )
    store.close()

    legacy_path = tmp_path / "legacy" / "workbench.sqlite"
    restored_path = tmp_path / "legacy-restored" / "workbench.sqlite"
    restored_path.parent.mkdir(parents=True)
    # fixture-write: Open the legacy fixture only after closing its original WorkspaceStore.
    legacy = sqlite3.connect(legacy_path)
    # fixture-write: Open a new isolated restore destination before its first WorkspaceStore.
    destination = sqlite3.connect(restored_path)
    try:
        # fixture-write: Remove review basis from the closed legacy database before restoring it.
        legacy.execute(
            "ALTER TABLE workbench_review_decision DROP COLUMN source_basis_digest"
        )
        # fixture-write: Remove source basis from the closed legacy database before restoring it.
        legacy.execute(
            "ALTER TABLE workbench_source_catalog DROP COLUMN content_basis_digest"
        )
        legacy.commit()  # fixture-write: Commit the offline legacy schema before copying the closed database.
        # fixture-write: Copy the offline legacy fixture after its store closed to a new isolated destination before its first WorkspaceStore.
        legacy.backup(destination)
    finally:
        destination.close()  # fixture-write: Close the isolated backup destination before its first WorkspaceStore is opened.
        legacy.close()  # fixture-write: Close the offline legacy migration connection after its backup completes.

    restored = WorkspaceStore(restored_path)
    try:
        recovered = restored.review_run(matter.matter_id, ACTOR, run.run_id)
        assert recovered.state == "failed"
        assert "predates exact source-content tracking" in recovered.message
        event = restored.connection.execute(
            "SELECT state,stage FROM workbench_review_event WHERE run_id=? "
            "ORDER BY ordinal DESC LIMIT 1",
            (run.run_id,),
        ).fetchone()
        assert event is not None
        assert event["state"] == "failed" and event["stage"] == "failed"
        assert "source_basis_digest" in {
            str(row[1])
            for row in restored.connection.execute(
                "PRAGMA table_info(workbench_review_decision)"
            )
        }
        assert restored.connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        restored.close()


def test_research_is_idempotent_checkpointed_recoverable_and_retryable(tmp_path):
    store, matter = _store(tmp_path)
    request_key = "research-request-" + "a" * 32
    first, created = store.queue_research_job(
        matter.matter_id, ACTOR, "What happened?", "Incident chronology", request_key
    )
    second, duplicated = store.queue_research_job(
        matter.matter_id, ACTOR, "What happened?", "Incident chronology", request_key
    )
    assert created is True and duplicated is False and first.job_id == second.job_id
    claimed = store.claim_research_job("research-worker-test")
    assert claimed is not None and claimed.job_id == first.job_id
    store.set_research_plan(first.job_id, {"queries": ["one", "two"]}, 4)
    store.update_research_progress(
        first.job_id,
        stage="searching",
        message="First pass complete.",
        completed_steps=1,
        candidate_count=12,
        evidence_count=5,
    )
    store.checkpoint_research_job(
        first.job_id,
        {"passes": [{"query": "one", "status": "supported"}], "evidence": []},
    )
    path = store.path
    store.close()

    restarted = WorkspaceStore(path)
    assert restarted.recover_running_research_jobs() == 1
    recovered = restarted.research_job(matter.matter_id, ACTOR, first.job_id)
    assert recovered.state == "queued" and recovered.completed_steps == 1
    assert recovered.result["passes"][0]["query"] == "one"
    assert restarted.claim_research_job("research-worker-two").job_id == first.job_id
    cancelling = restarted.cancel_research_job(matter.matter_id, ACTOR, first.job_id)
    assert cancelling.stage == "cancelling"
    cancelled = restarted.fail_research_job(first.job_id, "Research cancelled.")
    assert cancelled.state == "cancelled"
    retried = restarted.retry_research_job(matter.matter_id, ACTOR, first.job_id)
    assert retried.state == "queued" and retried.result == {} and retried.completed_steps == 0
    restarted.close()


def test_research_idempotency_key_is_bound_to_title_and_source_scope(tmp_path):
    store, matter = _store(tmp_path)
    source_set = store.create_source_set(
        matter.matter_id, "Generated scope", (f"{1:032x}",), ACTOR
    )
    request_key = "research-request-" + "b" * 32
    original, created = store.queue_research_job(
        matter.matter_id,
        ACTOR,
        "What happened?",
        "Generated scoped investigation",
        request_key,
        source_set.source_set_id,
    )
    repeated, duplicated = store.queue_research_job(
        matter.matter_id,
        ACTOR,
        "What happened?",
        "Generated scoped investigation",
        request_key,
        source_set.source_set_id,
    )
    assert created is True and duplicated is False
    assert repeated.job_id == original.job_id
    with pytest.raises(WorkspaceProblem, match="different investigation details"):
        store.queue_research_job(
            matter.matter_id,
            ACTOR,
            "What happened?",
            "Generated scoped investigation",
            request_key,
        )
    with pytest.raises(WorkspaceProblem, match="different investigation details"):
        store.queue_research_job(
            matter.matter_id,
            ACTOR,
            "What happened?",
            "Changed generated title",
            request_key,
            source_set.source_set_id,
        )
    store.close()


@pytest.mark.parametrize("source_change", ("version", "edit", "replacement"))
def test_resumed_research_revalidates_checkpoint_citations_after_source_change(
    tmp_path, monkeypatch, source_change
):
    app = create_workbench_app(
        tmp_path / "runtime",
        generator=EvidenceEchoGenerator(),
        auth_mode="test",
    )
    with TestClient(app) as client:
        bench = client.app.state.workbench
        assert bench.research is not None
        bench.research.close()
        principal = bench.workspace.upsert_principal(
            "test",
            ACTOR,
            "Workflow Reviewer",
            ACTOR,
            preferred_principal_id=ACTOR,
        )
        matter = bench.create_matter(
            "Generated checkpoint matter",
            "Synthetic citation-revalidation fixture",
            principal.principal_id,
        )
        source_store = bench.source_store(matter)
        original, _ = source_store.store_stream(
            "generated-chronology.txt",
            "text/plain",
            io.BytesIO(
                b"Generated chronology: device G-104 entered ready state at 09:14 "
                b"from the original checkpoint."
            ),
        )
        old_citation = bench.search(
            matter, "device G-104 ready state", limit=1
        )[0]
        queries = (
            "device G-104 ready state",
            "generated chronology G-104",
        )
        queued, created = bench.workspace.queue_research_job(
            matter.matter_id,
            ACTOR,
            "When did device G-104 enter ready state in the generated chronology?",
            "Generated device chronology",
            "research-request-" + "c" * 32,
        )
        assert created is True
        claimed = bench.workspace.claim_research_job("checkpoint-worker")
        assert claimed is not None and claimed.job_id == queued.job_id
        bench.workspace.set_research_plan(
            queued.job_id,
            {
                "title": "Generated device chronology",
                "objective": "Find the generated ready-state time.",
                "queries": list(queries),
            },
            len(queries) + 2,
        )
        bench.workspace.checkpoint_research_job(
            queued.job_id,
            {
                "passes": [
                    {
                        "query": queries[0],
                        "status": "supported",
                        "text": "Checkpoint-only stale finding at 09:14.",
                    }
                ],
                "evidence": [bench._workflow_citation_payload(old_citation)],
                "candidate_count": 37,
            },
        )
        assert bench.workspace.update_research_progress(
            queued.job_id,
            stage="searching",
            message="Saved one synthetic evidence pass.",
            completed_steps=1,
            candidate_count=37,
            evidence_count=1,
        )
        assert bench.workspace.recover_running_research_jobs() == 1

        if source_change == "version":
            original.version_id = "f" * 32
            # Persist the synthetic version transition through the same source
            # registry delta used by processing and transcript projection.
            source_store._save((original.document_id,))
            current = original
        elif source_change == "edit":
            edited_text = (
                "Generated chronology: device G-104 entered ready state at 09:22 "
                "after a reviewed source edit."
            )
            unit = asdict(original.parsed_units()[0])
            unit.update(
                {
                    "text": edited_text,
                    "excerpt_digest": hashlib.sha256(
                        edited_text.encode("utf-8")
                    ).hexdigest(),
                }
            )
            original.units = [unit]
            source_store._save((original.document_id,))
            current = original
        else:
            bench.remove_document(matter, source_store.action_token(original))
            current, _ = source_store.store_stream(
                "generated-chronology.txt",
                "text/plain",
                io.BytesIO(
                    b"Generated chronology: device G-104 entered ready state at 09:31 "
                    b"after a replacement upload."
                ),
            )
            bench._ensure_source_organizations(matter, source_store)

        with pytest.raises(KeyError):
            bench.support(matter, old_citation.support_token)

        search_calls: list[tuple[str, int]] = []
        original_answer_search = bench._answer_search

        def tracked_answer_search(*args, **kwargs):
            found = original_answer_search(*args, **kwargs)
            search_calls.append((str(args[2]), len(found)))
            return found

        monkeypatch.setattr(bench, "_answer_search", tracked_answer_search)
        resumed = bench.workspace.claim_research_job("resumed-worker")
        assert resumed is not None and resumed.job_id == queued.job_id
        result = bench._process_research_job(resumed, lambda: False)
        finished = bench.workspace.finish_research_job(queued.job_id, result)

        assert finished.state == "succeeded"
        assert [query for query, _ in search_calls] == list(queries)
        assert [item["query"] for item in result["passes"]] == list(queries)
        assert result["coverage"]["candidate_passage_count"] == sum(
            count for _, count in search_calls
        )
        assert "checkpoint-only stale finding" not in json.dumps(result).casefold()
        assert result["evidence"]
        for citation in result["evidence"]:
            assert citation["document_id"] == current.document_id
            assert citation["source_version_id"] == current.version_id
            assert citation["support_token"] != old_citation.support_token
            support = bench.support(matter, str(citation["support_token"]))
            assert support.source_name == current.display_name


def test_research_refuses_to_save_source_removed_during_the_same_run(
    tmp_path, monkeypatch
):
    app = create_workbench_app(
        tmp_path / "runtime",
        generator=EvidenceEchoGenerator(),
        auth_mode="test",
    )
    with TestClient(app) as client:
        bench = client.app.state.workbench
        assert bench.research is not None
        bench.research.close()
        principal = bench.workspace.upsert_principal(
            "test", ACTOR, "Workflow Reviewer", ACTOR, preferred_principal_id=ACTOR
        )
        matter = bench.create_matter(
            "Generated same-run source change",
            "Synthetic citation-revalidation fixture",
            principal.principal_id,
        )
        source_store = bench.source_store(matter)
        original, _ = source_store.store_stream(
            "generated-event.txt",
            "text/plain",
            io.BytesIO(b"Generated machine record: device K-12 changed state at 10:26."),
        )
        queued, created = bench.workspace.queue_research_job(
            matter.matter_id,
            ACTOR,
            "When did device K-12 change state?",
            "Generated state time",
            "research-request-" + "d" * 32,
        )
        assert created is True
        claimed = bench.workspace.claim_research_job("same-run-change-worker")
        assert claimed is not None and claimed.job_id == queued.job_id
        original_answer_search = bench._answer_search
        removed = False

        def remove_after_retrieval(*args, **kwargs):
            nonlocal removed
            found = original_answer_search(*args, **kwargs)
            if found and not removed:
                removed = True
                bench.remove_document(matter, source_store.action_token(original))
            return found

        monkeypatch.setattr(bench, "_answer_search", remove_after_retrieval)
        with pytest.raises(WorkflowFailure, match="source changed"):
            bench._process_research_job(claimed, lambda: False)
        failed = bench.workspace.fail_research_job(
            claimed.job_id,
            "A source changed while this investigation was working.",
        )
        assert failed.state == "failed" and failed.result_message_id is None


def test_research_revalidates_exact_sources_after_processing_before_save(tmp_path):
    app = create_workbench_app(
        tmp_path / "runtime",
        generator=EvidenceEchoGenerator(),
        auth_mode="test",
    )
    with TestClient(app) as client:
        bench = client.app.state.workbench
        assert bench.research is not None
        bench.research.close()
        bench.research = None
        created = client.post(
            "/matters",
            data={"name": "Generated finalization boundary", "descriptor": "Synthetic"},
            follow_redirects=False,
        )
        slug = created.headers["location"].split("/")[2]
        assert client.post(
            f"/matters/{slug}/uploads",
            files=[
                (
                    "files",
                    (
                        "generated-finalization.txt",
                        b"Generated machine record: device R-18 changed state at 07:46.",
                        "text/plain",
                    ),
                )
            ],
        ).status_code == 200
        matter = bench.workspace.get_active_matter(slug)
        source_store = bench.source_store(matter)
        document = next(iter(source_store.documents.values()))
        queued, _created = bench.workspace.queue_research_job(
            matter.matter_id,
            matter.owner_id,
            "When did device R-18 change state?",
            "Generated finalization check",
            "research-request-" + "9" * 32,
        )
        claimed = bench.workspace.claim_research_job("generated-finalization-worker")
        assert claimed is not None and claimed.job_id == queued.job_id
        result = bench._process_research_job(claimed, lambda: False)
        assert result["evidence"]

        bench.remove_document(matter, source_store.action_token(document))
        with pytest.raises(WorkflowFailure, match="source changed"):
            bench._finish_research_job(claimed, result)
        unfinished = bench.workspace.research_job(
            matter.matter_id, matter.owner_id, queued.job_id
        )
        assert unfinished.state == "running" and unfinished.result_message_id is None
        bench.workspace.fail_research_job(
            queued.job_id,
            "A source changed before this investigation could be saved.",
        )


def test_full_review_freezes_every_source_versions_criteria_and_computes_validation(tmp_path):
    store, matter = _store(tmp_path, count=120)
    criterion, version_one = store.create_review_criterion(
        matter.matter_id,
        ACTOR,
        title="Generated topic",
        instructions="Include a source that contains the generated topic.",
    )
    version_two = store.add_review_criterion_version(
        matter.matter_id,
        ACTOR,
        criterion.criterion_id,
        title="Generated topic",
        instructions="Include a source that expressly contains the generated topic.",
        exclude_guidance="Do not infer a match from the filename alone.",
    )
    assert version_one.version_number == 1 and version_two.version_number == 2
    current = store.review_criterion(matter.matter_id, criterion.criterion_id)
    assert current.version_count == 2 and current.current_version_id == version_two.criterion_version_id

    run = store.queue_review_run(
        matter.matter_id, ACTOR, version_two.criterion_version_id, run_kind="full"
    )
    assert run.snapshot_count == 120
    page = store.review_decisions(
        matter.matter_id, ACTOR, run.run_id, validation_only=True, page_size=100
    )
    assert page.total == 50
    assert store.claim_review_run("full-review-worker").run_id == run.run_id
    all_rows = store.review_decisions_for_export(matter.matter_id, ACTOR, run.run_id)
    for index, item in enumerate(all_rows):
        decision = "included" if index % 4 in {0, 1} else "excluded"
        store.record_review_decision(
            run.run_id, item.document_id, decision=decision, rationale="Generated decision"
        )
    finished = store.finish_review_run(run.run_id)
    assert finished.state == "succeeded" and finished.reviewed_count == 120

    sample = store.review_decisions(
        matter.matter_id, ACTOR, run.run_id, validation_only=True, page_size=100
    ).items
    # Use the saved machine labels to create TP, FP, FN, and TN exactly once.
    included = [item for item in sample if item.machine_decision == "included"][:2]
    excluded = [item for item in sample if item.machine_decision == "excluded"][:2]
    assert len(included) == 2 and len(excluded) == 2
    store.adjudicate_review_decision(
        matter.matter_id, ACTOR, run.run_id, included[0].document_id, expected_updated_at=included[0].updated_at, human_decision="agree"
    )
    store.adjudicate_review_decision(
        matter.matter_id, ACTOR, run.run_id, included[1].document_id, expected_updated_at=included[1].updated_at, human_decision="exclude"
    )
    store.adjudicate_review_decision(
        matter.matter_id, ACTOR, run.run_id, excluded[0].document_id, expected_updated_at=excluded[0].updated_at, human_decision="include"
    )
    store.adjudicate_review_decision(
        matter.matter_id, ACTOR, run.run_id, excluded[1].document_id, expected_updated_at=excluded[1].updated_at, human_decision="agree"
    )
    metrics = store.review_validation_metrics(matter.matter_id, ACTOR, run.run_id)
    assert metrics["true_positive"] == metrics["false_positive"] == 1
    assert metrics["false_negative"] == metrics["true_negative"] == 1
    assert metrics["precision"] == metrics["recall"] == pytest.approx(0.5)
    assert metrics["elusion"] == metrics["richness"] == metrics["error_rate"] == pytest.approx(0.5)
    store.close()


@pytest.mark.parametrize("source_change", ("before_review", "during_generation"))
def test_every_source_check_never_reviews_a_replacement_as_the_frozen_source(
    tmp_path, monkeypatch, source_change
):
    app = create_workbench_app(
        tmp_path / "runtime",
        generator=EvidenceEchoGenerator(),
        auth_mode="test",
    )
    with TestClient(app) as client:
        bench = client.app.state.workbench
        assert bench.full_review is not None
        bench.full_review.close()
        bench.full_review = None
        created = client.post(
            "/matters",
            data={"name": "Generated frozen source", "descriptor": "Synthetic fixture"},
            follow_redirects=False,
        )
        slug = re.search(
            r"/matters/(m-[0-9a-f]{12})/setup", created.headers["location"]
        ).group(1)
        uploaded = client.post(
            f"/matters/{slug}/uploads",
            files=[
                (
                    "files",
                    (
                        "generated-frozen-source.txt",
                        b"The generated source contains the bounded topic at 11:08.",
                        "text/plain",
                    ),
                )
            ],
        )
        assert uploaded.status_code == 200
        matter = bench.workspace.get_active_matter(slug)
        source_store = bench.source_store(matter)
        document = next(iter(source_store.documents.values()))
        old_version = document.version_id
        old_support = bench.search(matter, "bounded topic", limit=1)[0]
        _criterion, version = bench.workspace.create_review_criterion(
            matter.matter_id,
            matter.owner_id,
            title="Generated bounded topic",
            instructions="Include sources that contain the generated bounded topic.",
        )
        run = bench.workspace.queue_review_run(
            matter.matter_id,
            matter.owner_id,
            version.criterion_version_id,
            run_kind="full",
        )
        claimed = bench.workspace.claim_review_run("generated-frozen-worker")
        assert claimed is not None and claimed.run_id == run.run_id
        decision = bench.workspace.next_review_decision(run.run_id)
        assert decision is not None and decision.source_version_id == old_version

        def replace_frozen_version():
            document.version_id = "f" * 32
            source_store._save((document.document_id,))

        if source_change == "before_review":
            replace_frozen_version()
        else:
            original_classify = bench.generator.classify_source

            def change_during_generation(**kwargs):
                replace_frozen_version()
                return original_classify(**kwargs)

            monkeypatch.setattr(
                bench.generator, "classify_source", change_during_generation
            )

        result = bench._process_review_decision(claimed, decision, lambda: False)
        assert result.decision == "needs_attention"
        assert result.citations == ()
        assert "replacement" in result.rationale
        assert "not reviewed" in result.rationale or "no decision" in result.rationale
        bench.workspace.record_review_decision(
            run.run_id,
            decision.document_id,
            decision=result.decision,
            rationale=result.rationale,
            citations=result.citations,
            error_message=result.error_message,
        )
        saved = bench.workspace.review_decision(
            matter.matter_id, matter.owner_id, run.run_id, decision.document_id
        )
        assert saved.machine_decision == "needs_attention"
        assert saved.source_version_id == old_version
        assert saved.citations == ()
        with pytest.raises(KeyError):
            bench.support(matter, old_support.support_token)


def test_zero_hit_source_check_stays_unresolved_and_requires_staff_review(
    tmp_path, monkeypatch
):
    app = create_workbench_app(
        tmp_path / "runtime",
        generator=EvidenceEchoGenerator(),
        auth_mode="test",
    )
    with TestClient(app) as client:
        bench = client.app.state.workbench
        assert bench.full_review is not None
        bench.full_review.close()
        bench.full_review = None
        created = client.post(
            "/matters",
            data={"name": "Generated unresolved source", "descriptor": "Synthetic fixture"},
            follow_redirects=False,
        )
        slug = created.headers["location"].split("/")[2]
        assert client.post(
            f"/matters/{slug}/uploads",
            files=[
                (
                    "files",
                    (
                        "generated-unresolved.txt",
                        b"Generated source text without the saved criterion term.",
                        "text/plain",
                    ),
                )
            ],
        ).status_code == 200
        matter = bench.workspace.get_active_matter(slug)
        _criterion, version = bench.workspace.create_review_criterion(
            matter.matter_id,
            matter.owner_id,
            title="Generated absent term",
            instructions="Include sources containing the generated absent term.",
        )
        run = bench.workspace.queue_review_run(
            matter.matter_id,
            matter.owner_id,
            version.criterion_version_id,
            run_kind="full",
        )
        claimed = bench.workspace.claim_review_run("generated-unresolved-worker")
        assert claimed is not None
        decision = bench.workspace.next_review_decision(run.run_id)
        assert decision is not None
        monkeypatch.setattr(bench, "search", lambda *_args, **_kwargs: ())

        result = bench._process_review_decision(claimed, decision, lambda: False)

        assert result.decision == "needs_attention"
        assert result.citations == ()
        assert result.rationale == (
            "No matching passage was found by this search, so the source was not "
            "classified as included or excluded."
        )
        assert result.error_message == (
            "Review this source directly or refine the saved criterion, then run a "
            "new source check."
        )
        saved_run = bench._record_review_decision(claimed, decision, result)
        assert saved_run.attention_count == 1
        assert saved_run.excluded_count == 0
        saved = bench.workspace.review_decision(
            matter.matter_id, matter.owner_id, run.run_id, decision.document_id
        )
        assert saved.machine_decision == "needs_attention"
        assert saved.citations == ()


def test_zero_hit_source_check_detects_same_version_content_change_before_save(
    tmp_path, monkeypatch
):
    app = create_workbench_app(
        tmp_path / "runtime",
        generator=EvidenceEchoGenerator(),
        auth_mode="test",
    )
    with TestClient(app) as client:
        bench = client.app.state.workbench
        assert bench.full_review is not None
        bench.full_review.close()
        bench.full_review = None
        created = client.post(
            "/matters",
            data={"name": "Generated zero-hit source", "descriptor": "Synthetic fixture"},
            follow_redirects=False,
        )
        slug = created.headers["location"].split("/")[2]
        assert client.post(
            f"/matters/{slug}/uploads",
            files=[
                (
                    "files",
                    (
                        "generated-zero-hit.txt",
                        b"Generated source text before a reviewed correction.",
                        "text/plain",
                    ),
                )
            ],
        ).status_code == 200
        matter = bench.workspace.get_active_matter(slug)
        store = bench.source_store(matter)
        document = next(iter(store.documents.values()))
        _criterion, version = bench.workspace.create_review_criterion(
            matter.matter_id,
            matter.owner_id,
            title="Generated absent term",
            instructions="Include sources containing the generated absent term.",
        )
        run = bench.workspace.queue_review_run(
            matter.matter_id,
            matter.owner_id,
            version.criterion_version_id,
            run_kind="full",
        )
        claimed = bench.workspace.claim_review_run("generated-zero-hit-worker")
        assert claimed is not None
        decision = bench.workspace.next_review_decision(run.run_id)
        assert decision is not None and decision.source_basis_digest
        monkeypatch.setattr(bench, "search", lambda *_args, **_kwargs: ())
        result = bench._process_review_decision(claimed, decision, lambda: False)
        assert result.decision == "needs_attention" and result.citations == ()

        changed_text = "Generated corrected text added after the zero-hit decision."
        unit = asdict(document.parsed_units()[0])
        unit.update(
            {
                "text": changed_text,
                "excerpt_digest": hashlib.sha256(
                    changed_text.encode("utf-8")
                ).hexdigest(),
            }
        )
        document.units = [unit]
        with store.mutation_guard():
            store._save((document.document_id,))
        bench._record_review_decision(claimed, decision, result)
        saved = bench.workspace.review_decision(
            matter.matter_id, matter.owner_id, run.run_id, decision.document_id
        )
        assert saved.machine_decision == "needs_attention"
        assert saved.citations == ()
        assert "replacement was not reviewed" in saved.rationale


def test_source_check_revalidates_same_version_content_after_decision_before_finish(
    tmp_path, monkeypatch
):
    app = create_workbench_app(
        tmp_path / "runtime",
        generator=EvidenceEchoGenerator(),
        auth_mode="test",
    )
    with TestClient(app) as client:
        bench = client.app.state.workbench
        assert bench.full_review is not None
        bench.full_review.close()
        bench.full_review = None
        created = client.post(
            "/matters",
            data={"name": "Generated finish boundary", "descriptor": "Synthetic"},
            follow_redirects=False,
        )
        slug = created.headers["location"].split("/")[2]
        assert client.post(
            f"/matters/{slug}/uploads",
            files=[
                (
                    "files",
                    (
                        "generated-finish-boundary.txt",
                        b"Generated source text before final source-check validation.",
                        "text/plain",
                    ),
                )
            ],
        ).status_code == 200
        matter = bench.workspace.get_active_matter(slug)
        source_store = bench.source_store(matter)
        document = next(iter(source_store.documents.values()))
        _criterion, version = bench.workspace.create_review_criterion(
            matter.matter_id,
            matter.owner_id,
            title="Generated finish-boundary criterion",
            instructions="Include sources containing a generated absent term.",
        )
        run = bench.workspace.queue_review_run(
            matter.matter_id,
            matter.owner_id,
            version.criterion_version_id,
            run_kind="full",
        )
        claimed = bench.workspace.claim_review_run("generated-finish-boundary-worker")
        assert claimed is not None and claimed.run_id == run.run_id
        decision = bench.workspace.next_review_decision(run.run_id)
        assert decision is not None and decision.source_basis_digest
        monkeypatch.setattr(bench, "search", lambda *_args, **_kwargs: ())
        result = bench._process_review_decision(claimed, decision, lambda: False)
        assert result.decision == "needs_attention"
        bench._record_review_decision(claimed, decision, result)

        changed_text = "Generated corrected text after the saved source decision."
        unit = asdict(document.parsed_units()[0])
        unit.update(
            {
                "text": changed_text,
                "excerpt_digest": hashlib.sha256(
                    changed_text.encode("utf-8")
                ).hexdigest(),
            }
        )
        document.units = [unit]
        with source_store.mutation_guard():
            source_store._save((document.document_id,))

        finished = bench._finish_review_run(claimed)
        assert finished.state == "succeeded"
        assert finished.attention_count == 1 and finished.excluded_count == 0
        saved = bench.workspace.review_decision(
            matter.matter_id, matter.owner_id, run.run_id, decision.document_id
        )
        assert saved.machine_decision == "needs_attention"
        assert saved.citations == ()
        assert "replacement was not reviewed" in saved.rationale


def test_full_review_coordinator_isolates_one_source_failure(tmp_path):
    store, matter = _store(tmp_path, count=3)
    criterion, version = store.create_review_criterion(
        matter.matter_id,
        ACTOR,
        title="Generated topic",
        instructions="Include generated topic material.",
    )
    run = store.queue_review_run(
        matter.matter_id, ACTOR, version.criterion_version_id, run_kind="full"
    )
    failed_document = f"{2:032x}"

    def process(_run, decision, _cancelled):
        if decision.document_id == failed_document:
            raise RuntimeError("generated source-level failure")
        return ReviewDecisionResult("included", "Generated supported result")

    coordinator = ReviewCoordinator(store, process=process, workers=1)
    coordinator.notify()
    deadline = time.monotonic() + 5
    finished = run
    while time.monotonic() < deadline:
        finished = store.review_run(matter.matter_id, ACTOR, run.run_id)
        if finished.state == "succeeded":
            break
        time.sleep(0.01)
    assert finished.state == "succeeded"
    assert finished.included_count == 2 and finished.attention_count == 1
    failed = store.review_decision(matter.matter_id, ACTOR, run.run_id, failed_document)
    assert failed.machine_decision == "needs_attention"
    assert "other sources continued" in failed.error_message
    coordinator.close()
    store.close()


def test_revoked_member_cannot_complete_a_processed_source_check(tmp_path):
    store, matter = _store(tmp_path, count=1)
    member = store.upsert_principal(
        "test",
        "review-member",
        "Review Member",
        "review-member",
        preferred_principal_id="principal-review-member",
    )
    store.add_member(matter.matter_id, member.principal_id, matter.owner_id)
    _criterion, version = store.create_review_criterion(
        matter.matter_id,
        member.principal_id,
        title="Generated member criterion",
        instructions="Include the generated source.",
    )
    run = store.queue_review_run(
        matter.matter_id,
        member.principal_id,
        version.criterion_version_id,
        run_kind="full",
    )
    claimed = store.claim_review_run("generated-member-review-worker")
    assert claimed is not None and claimed.run_id == run.run_id
    decision = store.next_review_decision(run.run_id)
    assert decision is not None
    store.record_review_decision(
        run.run_id,
        decision.document_id,
        decision="excluded",
        rationale="Generated processed decision.",
    )
    store.revoke_member(matter.matter_id, member.principal_id, matter.owner_id)

    with pytest.raises(WorkspaceProblem, match="Access to this matter was removed"):
        store.finish_review_run(run.run_id)
    durable = store.review_run(matter.matter_id, matter.owner_id, run.run_id)
    assert durable.state == "running"
    assert store.fail_review_run(
        run.run_id, "Access to this matter was removed."
    ).state == "failed"
    store.close()


def test_full_review_honors_a_frozen_source_set(tmp_path):
    store, matter = _store(tmp_path, count=20)
    scoped_ids = tuple(f"{index:032x}" for index in range(1, 8))
    source_set = store.create_source_set(
        matter.matter_id, "Generated seven", scoped_ids, ACTOR
    )
    _criterion, version = store.create_review_criterion(
        matter.matter_id,
        ACTOR,
        title="Scoped generated topic",
        instructions="Include generated topic material.",
    )
    run = store.queue_review_run(
        matter.matter_id,
        ACTOR,
        version.criterion_version_id,
        run_kind="full",
        source_set_id=source_set.source_set_id,
    )
    frozen = store.review_decisions_for_export(matter.matter_id, ACTOR, run.run_id)
    assert run.snapshot_count == 7
    assert {item.document_id for item in frozen} == set(scoped_ids)
    # Later changes to the saved set do not mutate an already frozen run.
    store.add_sources_to_set(
        matter.matter_id, source_set.source_set_id, (f"{8:032x}",), ACTOR
    )
    frozen_again = store.review_decisions_for_export(matter.matter_id, ACTOR, run.run_id)
    assert {item.document_id for item in frozen_again} == set(scoped_ids)
    store.close()


def test_full_review_uses_two_bounded_source_slots(tmp_path):
    store, matter = _store(tmp_path, count=8)
    _criterion, version = store.create_review_criterion(
        matter.matter_id,
        ACTOR,
        title="Bounded concurrency",
        instructions="Include generated topic material.",
    )
    run = store.queue_review_run(
        matter.matter_id, ACTOR, version.criterion_version_id, run_kind="full"
    )
    lock = threading.Lock()
    active = 0
    maximum_active = 0

    def process(_run, _decision, _cancelled):
        nonlocal active, maximum_active
        with lock:
            active += 1
            maximum_active = max(maximum_active, active)
        time.sleep(0.03)
        with lock:
            active -= 1
        return ReviewDecisionResult("included", "Generated supported result")

    coordinator = ReviewCoordinator(
        store, process=process, workers=1, source_concurrency=2
    )
    coordinator.notify()
    deadline = time.monotonic() + 5
    finished = run
    while time.monotonic() < deadline:
        finished = store.review_run(matter.matter_id, ACTOR, run.run_id)
        if finished.state == "succeeded":
            break
        time.sleep(0.01)
    assert finished.state == "succeeded"
    assert finished.reviewed_count == 8
    assert maximum_active == 2
    coordinator.close()
    store.close()


def test_review_workspace_runs_exports_and_final_bundle_include_new_work_product(tmp_path, monkeypatch):
    runtime = tmp_path / "runtime"
    with TestClient(
        create_workbench_app(runtime, generator=EvidenceEchoGenerator(), auth_mode="test")
    ) as client:
        progress_messages = []
        update_progress = client.app.state.workbench.workspace.update_research_progress

        def record_progress(*args, **kwargs):
            progress_messages.append(kwargs.get("message", ""))
            return update_progress(*args, **kwargs)

        monkeypatch.setattr(client.app.state.workbench.workspace, "update_research_progress", record_progress)
        created = client.post(
            "/matters",
            data={"name": "Generated web workflow", "descriptor": "Public fixture"},
            follow_redirects=False,
        )
        slug = re.search(
            r"/matters/(m-[0-9a-f]{12})/setup", created.headers["location"]
        ).group(1)
        uploaded = client.post(
            f"/matters/{slug}/uploads",
            files=[
                (
                    "files",
                    (
                        "generated-source.txt",
                        b"The generated source says the blue vehicle arrived at noon.",
                        "text/plain",
                    ),
                )
            ],
        )
        assert uploaded.status_code == 200
        quick = client.get(f"/matters/{slug}")
        assert "Review" in quick.text
        assert "Investigate more deeply" in quick.text
        assert "<span>Review</span>" in quick.text
        assert "Quick answer" not in quick.text
        assert "Deep research" not in quick.text

        conversation_id = re.search(
            r'name="conversation" value="([^"]+)"', quick.text
        ).group(1)
        key = re.search(r'name="request_key" value="([^"]+)"', quick.text).group(1)
        research_start = client.post(
            f"/matters/{slug}/ask",
            data={
                "question": "What does the record say about the blue vehicle?",
                "conversation": conversation_id,
                "request_key": key,
                "source_set": "",
                "notebook_mode": "",
                "review_task": "research",
            },
            follow_redirects=False,
        )
        assert research_start.status_code == 303
        matter = client.app.state.workbench.workspace.get_active_matter(slug)
        research_id = client.app.state.workbench.workspace.research_jobs(
            matter.matter_id, matter.owner_id
        )[0].job_id
        research_url = f"/matters/{slug}/research?job={research_id}"
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            research_result = client.get(research_url)
            if 'aria-label="Completed synthesis actions"' in research_result.text:
                break
            time.sleep(0.02)
        assert "Evidence record" in research_result.text
        assert "verifies the synthesis" not in research_result.text
        assert "Generated synthesis · needs review" in research_result.text
        assert "Citations and automated checks do not establish" in research_result.text
        assert "Final verification complete" not in "\n".join(progress_messages)
        assert any("Citation references checked" in message for message in progress_messages)
        assert "Return to conversation" in research_result.text
        research_status = client.get(
            f"/matters/{slug}/research/{research_id}/status"
        )
        assert research_status.status_code == 200
        assert not ({"summary", "evidence", "question"} & research_status.json().keys())
        research_export = re.search(
            r'href="([^"]+/research-[^/]+/export\?format=json)"', research_result.text
        )
        assert research_export
        research_download = client.get(research_export.group(1))
        assert research_download.status_code == 200
        portable_research = research_download.json()
        assert portable_research["matter"] == {"name": matter.display_name}
        assert portable_research["investigation"]["supporting_sources"]
        assert all(
            item["location"]
            for item in portable_research["investigation"]["supporting_sources"]
        )
        serialized_research = json.dumps(portable_research).casefold()
        for forbidden in (
            "support_token",
            "excerpt_digest",
            "matter_id",
            "document_id",
            "source_version_id",
            "chunk_id",
            "model_called",
            "elapsed_ms",
            "rerank",
        ):
            assert forbidden not in serialized_research

        criterion_create = client.post(
            f"/matters/{slug}/full-review/criteria",
            data={
                "title": "Vehicle references",
                "instructions": "Include sources that mention the blue vehicle.",
                "include_guidance": "An express blue vehicle reference.",
                "exclude_guidance": "No express vehicle reference.",
            },
            follow_redirects=False,
        )
        criterion_page = client.get(criterion_create.headers["location"])
        version_id = re.search(
            r'name="criterion_version_id" value="([^"]+)"', criterion_page.text
        ).group(1)
        review_start = client.post(
            f"/matters/{slug}/full-review/runs",
            data={"criterion_version_id": version_id, "run_kind": "sample", "source_set": ""},
            follow_redirects=False,
        )
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            review_result = client.get(review_start.headers["location"])
            if "Every-source check complete:" in review_result.text:
                break
            time.sleep(0.02)
        assert "Every-source ledger" in review_result.text
        review_id = re.search(
            r"run=(review-run-[0-9a-f]{32})", review_start.headers["location"]
        ).group(1)
        review_status = client.get(f"/matters/{slug}/full-review/{review_id}/status")
        assert review_status.status_code == 200
        assert not ({"rationale", "citations", "source_name"} & review_status.json().keys())
        csv_url = re.search(r'href="([^"]+/export\?format=csv)"', review_result.text).group(1)
        csv_export = client.get(csv_url)
        assert csv_export.status_code == 200 and f"{LEGACY_PRODUCT} label" in csv_export.text

        second = client.post(
            "/matters",
            data={"name": "Generated isolation matter", "descriptor": "Public fixture"},
            follow_redirects=False,
        )
        second_slug = re.search(
            r"/matters/(m-[0-9a-f]{12})/setup", second.headers["location"]
        ).group(1)
        assert client.get(
            f"/matters/{second_slug}/research/{research_id}/status"
        ).status_code == 404
        assert client.get(
            f"/matters/{second_slug}/research/{research_id}/export?format=json"
        ).status_code == 404
        assert client.get(
            f"/matters/{second_slug}/full-review/{review_id}/status"
        ).status_code == 404
        assert client.get(
            f"/matters/{second_slug}/full-review/{review_id}/export?format=csv"
        ).status_code == 404

        bundle = client.get(f"/matters/{slug}/export")
        assert bundle.status_code == 200
        import io
        import zipfile

        with zipfile.ZipFile(io.BytesIO(bundle.content)) as archive:
            names = archive.namelist()
            assert any(
                name.startswith("investigations/") and name.endswith(".json")
                for name in names
            )
            assert any(
                name.startswith("source-checks/") and name.endswith(".csv")
                for name in names
            )

        durable_research = client.app.state.workbench.workspace.research_job(
            matter.matter_id, matter.owner_id, research_id
        )
        original_result = json.loads(json.dumps(durable_research.result))

        def assert_corruption_is_refused(
            corrupt_result: dict[str, object], marker: str
        ) -> None:
            with fixture_write(client.app.state.workbench.workspace):
                client.app.state.workbench.workspace.connection.execute(
                    "UPDATE workbench_research_job SET result_json=? WHERE job_id=?",
                    (json.dumps(corrupt_result), research_id),
                )
            for format_name in ("json", "markdown", "docx"):
                refused = client.get(
                    f"/matters/{slug}/research/{research_id}/export",
                    params={"format": format_name},
                )
                assert refused.status_code == 409
                assert marker not in refused.text
            refused_bundle = client.get(f"/matters/{slug}/export")
            assert refused_bundle.status_code == 409
            assert marker.encode("utf-8") not in refused_bundle.content

        foreign_marker = "GENERATED-FOREIGN-MATTER-EXCERPT-MUST-NOT-EXPORT"
        corrupt_result = json.loads(json.dumps(original_result))
        corrupt_result["evidence"][0]["matter_id"] = (
            client.app.state.workbench.workspace.get_active_matter(second_slug).matter_id
        )
        corrupt_result["evidence"][0]["excerpt"] = foreign_marker
        assert_corruption_is_refused(corrupt_result, foreign_marker)

        forged_marker = "GENERATED-COHERENT-FORGED-LOCATOR-MUST-NOT-EXPORT"
        forged = json.loads(json.dumps(original_result))
        original_token = forged["evidence"][0]["support_token"]
        forged_token = "f" * 40

        def replace_locator(value: object) -> None:
            if isinstance(value, dict):
                if value.get("support_token") == original_token:
                    value["support_token"] = forged_token
                    value["source_name"] = "generated-forged-source.txt"
                    value["location"] = "Line 999"
                    if "href" in value:
                        value["href"] = f"?support={forged_token}"
                    if "excerpt" in value:
                        value["excerpt"] = forged_marker
                        value["excerpt_digest"] = hashlib.sha256(
                            forged_marker.encode("utf-8")
                        ).hexdigest()
                        value["chunk_id"] = "chunk-999"
                        value["unit_number"] = 999
                        value["line_start"] = 999
                        value["line_end"] = 999
                for nested in value.values():
                    replace_locator(nested)
            elif isinstance(value, list):
                for nested in value:
                    replace_locator(nested)

        replace_locator(forged)
        assert_corruption_is_refused(forged, forged_marker)

        synthesis_marker = "GENERATED-REPLACEMENT-SYNTHESIS-MUST-NOT-EXPORT"
        corrupt_summary = json.loads(json.dumps(original_result))
        corrupt_summary["summary"] = synthesis_marker
        assert_corruption_is_refused(corrupt_summary, synthesis_marker)

        finding_marker = "GENERATED-REPLACEMENT-FINDING-MUST-NOT-EXPORT"
        corrupt_finding = json.loads(json.dumps(original_result))
        corrupt_finding["passes"][0]["text"] = finding_marker
        assert_corruption_is_refused(corrupt_finding, finding_marker)

        gap_marker = "GENERATED-REPLACEMENT-GAP-MUST-NOT-EXPORT"
        corrupt_gap = json.loads(json.dumps(original_result))
        corrupt_gap["passes"][0].pop("answer", None)
        corrupt_gap["passes"][0]["status"] = "gap"
        corrupt_gap["passes"][0]["text"] = gap_marker
        assert_corruption_is_refused(corrupt_gap, gap_marker)

        with fixture_write(client.app.state.workbench.workspace):
            client.app.state.workbench.workspace.connection.execute(
                "UPDATE workbench_research_job SET result_json=? WHERE job_id=?",
                (json.dumps(original_result), research_id),
            )
