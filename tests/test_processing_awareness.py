from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from case_intelligence.generation import UnavailableGenerator
from case_intelligence.workbench import create_workbench_app
from case_intelligence.workspace_store import WorkspaceProblem, WorkspaceStore


ACTOR = "development-taylor-morgan"


def _seed_store(tmp_path):
    store = WorkspaceStore(tmp_path / "workspace.sqlite")
    store.upsert_principal(
        "test", "owner", "Owner Example", "owner", preferred_principal_id=ACTOR
    )
    matter = store.create_matter("Generated readiness matter", "Synthetic", ACTOR)
    return store, matter


def _catalog_source(document_id: str, *, tone: str, state: str) -> dict[str, object]:
    suffix = document_id[-8:]
    return {
        "document_id": document_id,
        "version_id": ("1" * 24) + suffix,
        "action_token": ("2" * 24) + suffix,
        "display_name": f"generated-{suffix}.txt",
        "relative_path": f"generated/{suffix}.txt",
        "media_type": "text/plain",
        "kind": "TXT",
        "source_state": state,
        "tone": tone,
        "state_label": "Searchable" if tone == "ready" else "Processing",
        "count_label": "1 line",
        "processing_stage": "Indexing for search" if state == "ready" else "Queued",
        "completed_units": 1 if state == "ready" else 0,
        "total_units": 1,
        "page_count": 0,
        "duration_ms": 0,
        "byte_size": 40,
        "origin": "upload",
        "retryable": False,
        "removable": tone != "processing",
        "has_video": False,
    }


def test_readiness_uses_durable_jobs_not_optimistic_catalog_state(tmp_path):
    store, matter = _seed_store(tmp_path)
    indexing_id = "1" * 32
    queued_id = "2" * 32
    ready_id = "3" * 32
    store.upsert_source_catalog(
        matter.matter_id,
        (
            _catalog_source(indexing_id, tone="ready", state="ready"),
            _catalog_source(queued_id, tone="processing", state="queued"),
            _catalog_source(ready_id, tone="ready", state="ready"),
        ),
    )

    indexing = store.queue_upload(matter.matter_id, indexing_id)
    assert store.claim_ingest_job("generated-worker").job_id == indexing.job_id
    store.update_ingest_job(
        indexing.job_id,
        stage="Indexing for search",
        completed_units=1,
        total_units=1,
    )
    queued = store.queue_upload(matter.matter_id, queued_id)

    readiness = store.matter_readiness(matter.matter_id)
    assert readiness.state == "preparing"
    assert readiness.total_count == 3
    assert readiness.saved_count == 3
    assert readiness.extracted_count == 2
    assert readiness.searchable_count == 1
    assert readiness.processing_count == 2
    assert readiness.attention_count == 0
    assert readiness.indexing_count == 1
    assert readiness.extracting_count == 1
    assert not readiness.can_query

    store.finish_ingest_job(indexing.job_id, succeeded=True)
    assert store.claim_ingest_job("generated-worker").job_id == queued.job_id
    store.finish_ingest_job(
        queued.job_id, succeeded=False, message="Generated OCR failure"
    )
    attention = store.matter_readiness(matter.matter_id)
    assert attention.state == "attention"
    assert attention.searchable_count == 2
    assert attention.processing_count == 0
    assert attention.attention_count == 1
    assert attention.can_query
    assert attention.partial_query
    store.close()


def test_attention_blocks_only_when_no_source_is_searchable(tmp_path):
    store, matter = _seed_store(tmp_path)
    document_id = "9" * 32
    store.upsert_source_catalog(
        matter.matter_id,
        (_catalog_source(document_id, tone="ready", state="ready"),),
    )
    queued = store.queue_upload(matter.matter_id, document_id)
    assert store.claim_ingest_job("generated-worker").job_id == queued.job_id
    store.finish_ingest_job(
        queued.job_id, succeeded=False, message="Generated extraction failure"
    )

    attention = store.matter_readiness(matter.matter_id)
    assert attention.state == "attention"
    assert attention.searchable_count == 0
    assert attention.attention_count == 1
    assert not attention.can_query
    assert not attention.partial_query
    store.close()


def test_first_draft_question_is_atomic_and_idempotent(tmp_path):
    store, matter = _seed_store(tmp_path)
    before = len(store.conversations(matter.matter_id))
    key = "answer-request-" + "a" * 32

    def submit():
        return store.queue_answer_job(
            matter.matter_id,
            None,
            ACTOR,
            "What does the generated record show?",
            key,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = tuple(pool.map(lambda _ordinal: submit(), range(2)))
    assert {job.job_id for job, _created in results} == {results[0][0].job_id}
    assert sorted(created for _job, created in results) == [False, True]
    job = results[0][0]
    assert len(store.conversations(matter.matter_id)) == before + 1
    conversation = store.get_conversation(matter.matter_id, job.conversation_id)
    assert conversation.title == "What does the generated record show?"
    assert [message.content for message in store.messages(
        matter.matter_id, conversation.conversation_id
    )] == ["What does the generated record show?"]

    conversation_count = len(store.conversations(matter.matter_id))
    with pytest.raises(WorkspaceProblem, match="empty or no longer available"):
        store.queue_answer_job(
            matter.matter_id,
            None,
            ACTOR,
            "This transaction must roll back.",
            "answer-request-" + "b" * 32,
            "source-set-" + "c" * 32,
        )
    assert len(store.conversations(matter.matter_id)) == conversation_count
    store.close()


def test_first_draft_question_is_idempotent_across_store_connections(tmp_path):
    primary, matter = _seed_store(tmp_path)
    secondary = WorkspaceStore(primary.path)
    barrier = threading.Barrier(2)
    key = "answer-request-" + "f" * 32

    def submit(store: WorkspaceStore):
        barrier.wait(timeout=5)
        return store.queue_answer_job(
            matter.matter_id,
            None,
            ACTOR,
            "What does this generated cross-connection record show?",
            key,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        pending = [pool.submit(submit, store) for store in (primary, secondary)]
        results = tuple(future.result() for future in pending)
    assert {job.job_id for job, _created in results} == {results[0][0].job_id}
    assert {job.conversation_id for job, _created in results} == {
        results[0][0].conversation_id
    }
    assert sorted(created for _job, created in results) == [False, True]
    assert len(primary.conversations(matter.matter_id)) == 2
    secondary.close()
    primary.close()


def test_http_status_gates_search_and_draft_questions_until_ready(tmp_path):
    app = create_workbench_app(
        tmp_path / "runtime", generator=UnavailableGenerator(), auth_mode="test"
    )
    with TestClient(app) as client:
        created = client.post(
            "/matters",
            data={"name": "Generated HTTP readiness", "descriptor": "Synthetic"},
            follow_redirects=False,
        )
        slug = created.headers["location"].split("/")[2]
        bench = client.app.state.workbench
        matter = bench.matter(slug, ACTOR)
        conversation_count = len(bench.workspace.conversations(matter.matter_id))

        status = client.get(f"/matters/{slug}/processing-status")
        assert status.status_code == 200
        assert status.json()["state"] == "empty"
        assert status.json()["can_query"] is False

        blocked = client.post(
            f"/matters/{slug}/ask",
            data={
                "conversation": "",
                "question": "What is in the generated matter?",
                "request_key": "answer-request-" + "d" * 32,
            },
            headers={"Accept": "application/json"},
        )
        assert blocked.status_code == 409
        assert "source" in blocked.json()["message"].casefold()
        assert len(bench.workspace.conversations(matter.matter_id)) == conversation_count

        secret_name = "generated-sensitive-name.txt"
        source = _catalog_source("4" * 32, tone="ready", state="ready")
        source["display_name"] = secret_name
        source["relative_path"] = f"generated/{secret_name}"
        bench.workspace.upsert_source_catalog(matter.matter_id, (source,))
        ready = client.get(f"/matters/{slug}/processing-status")
        payload = ready.json()
        assert payload["state"] == "ready"
        assert payload["can_query"] is True
        assert secret_name not in json.dumps(payload)

        failed_name = "generated-sensitive-failed-name.txt"
        failed_id = "5" * 32
        failed_source = _catalog_source(failed_id, tone="processing", state="queued")
        failed_source["display_name"] = failed_name
        failed_source["relative_path"] = f"generated/{failed_name}"
        bench.workspace.upsert_source_catalog(matter.matter_id, (failed_source,))
        failed_job = bench.workspace.queue_upload(matter.matter_id, failed_id)
        claimed = bench.workspace.claim_ingest_job("generated-http-worker")
        assert claimed is not None and claimed.job_id == failed_job.job_id
        bench.workspace.finish_ingest_job(
            failed_job.job_id,
            succeeded=False,
            message="Generated extraction failure",
        )

        partial_status = client.get(f"/matters/{slug}/processing-status")
        partial = partial_status.json()
        assert partial_status.status_code == 200
        assert partial["state"] == "attention"
        assert partial["can_query"] is True
        assert partial["partial_query"] is True
        assert partial["searchable_count"] == 1
        assert partial["total_count"] == 2
        assert partial["excluded_count"] == 1
        assert partial["coverage_notice"] == (
            "Searchable text is available for 1 of 2 sources. "
            "1 source needs attention and is excluded."
        )
        assert failed_name not in json.dumps(partial)

        review = client.get(f"/matters/{slug}")
        assert review.status_code == 200
        assert "Review available with exclusions" in review.text
        assert partial["coverage_notice"] in review.text
        assert 'id="matter-question"' in review.text
        assert 'data-sources-ready="true"' in review.text

        accepted = client.post(
            f"/matters/{slug}/ask",
            data={
                "conversation": "",
                "question": "What is in the generated matter?",
                "request_key": "answer-request-" + "e" * 32,
            },
            headers={"Accept": "application/json"},
        )
        assert accepted.status_code == 202
        answer = accepted.json()
        assert answer["conversation_id"].startswith("conversation-")
        assert answer["fragment_url"].startswith(f"/matters/{slug}/assistant?")
        assert len(bench.workspace.conversations(matter.matter_id)) == conversation_count + 1

        home = client.get(f"/matters/{slug}/home")
        assert 'data-matter-readiness' in home.text
        assert "Review available with exclusions" in home.text
        assert "View processing details" not in home.text


@pytest.mark.parametrize('recording_counter', ['playback_only_count', 'recording_review_count'])
def test_pending_upload_coverage_retains_recording_review_recovery(tmp_path, recording_counter):
    from dataclasses import replace
    from case_intelligence.workbench import _source_coverage
    store, matter = _seed_store(tmp_path)
    readiness = replace(store.matter_readiness(matter.matter_id), state='preparing',
        total_count=3, searchable_count=1, attention_count=1, processing_count=1,
        uploading_count=1, **{recording_counter: 1})
    coverage = _source_coverage(readiness)
    assert coverage['mode'] == 'partial' and coverage['excluded_count'] == 2
    assert 'still uploading or processing' in coverage['notice']
    assert 'remain available for playback and review' in coverage['notice']
    store.close()
