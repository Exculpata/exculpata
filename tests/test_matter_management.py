from __future__ import annotations

from tests.fixture_write import fixture_write


from case_intelligence.legacy_names import (
    LEGACY_STEM,
)
from tests.case_lifecycle_helpers import lifecycle_session

import io
import json
import os
import re
import sqlite3
import threading
import zipfile

import pytest
from fastapi.testclient import TestClient

import case_intelligence.workbench as workbench_module
import case_intelligence.work_product_exports as export_module
from case_intelligence.generation import UnavailableGenerator
from case_intelligence.identity import (
    KERBEROS_SECRET_HEADER,
    KERBEROS_USER_HEADER,
    KerberosSettings,
)
from case_intelligence.pilot_uploads import UploadProblem
from case_intelligence.workbench import CaseIntelligenceWorkbench
from case_intelligence.workbench import create_workbench_app
from case_intelligence.workspace_store import WorkspaceProblem, WorkspaceStore
from tests.case_lifecycle_helpers import prepare_purge_confirmation
from tests.case_lifecycle_bench_helpers import prepare_kerberos_exit
from tests.case_lifecycle_web_helpers import prepare_bench_exit


PROXY_SECRET = f"synthetic_{LEGACY_STEM}_proxy_secret_0000000000000000"
REALM = "EXAMPLE.TEST"
ADMIN = "admin.reviewer@EXAMPLE.TEST"
OWNER = "matter.owner@EXAMPLE.TEST"
OTHER = "other.reviewer@EXAMPLE.TEST"
USER_GROUP = f"{LEGACY_STEM}_users@example.test"
ADMIN_GROUP = f"{LEGACY_STEM}_administrators@example.test"


def _headers(principal: str) -> dict[str, str]:
    return {
        KERBEROS_USER_HEADER: principal,
        KERBEROS_SECRET_HEADER: PROXY_SECRET,
    }


def _profile(principal: str) -> tuple[str, str]:
    local = principal.split("@", 1)[0]
    return " ".join(part.capitalize() for part in local.split(".")), principal.casefold()


def _csrf(page: str) -> str:
    matched = re.search(r'data-csrf-token="([0-9a-f]{64})"', page)
    assert matched is not None
    return matched.group(1)


def _principal_id(client: TestClient, subject: str) -> str:
    row = client.app.state.workbench.workspace.connection.execute(
        "SELECT principal_id FROM workbench_principal WHERE provider_subject=?",
        (subject,),
    ).fetchone()
    assert row is not None
    return str(row[0])


def _create_matter(
    client: TestClient, *, principal: str, csrf_token: str, name: str
) -> str:
    response = client.post(
        "/matters",
        data={
            "csrf_token": csrf_token,
            "name": name,
            "descriptor": "Generated authorization fixture",
        },
        headers=_headers(principal),
        follow_redirects=False,
    )
    assert response.status_code == 303
    return response.headers["location"].split("/")[2]


def _app(tmp_path):
    memberships = {
        ADMIN: {ADMIN_GROUP},
        OWNER: {USER_GROUP},
        OTHER: {USER_GROUP},
    }
    return create_workbench_app(
        tmp_path / "runtime",
        generator=UnavailableGenerator(),
        auth_mode="kerberos",
        secure_cookie=True,
        kerberos_settings=KerberosSettings(
            realm=REALM,
            proxy_secret=PROXY_SECRET,
            allowed_groups=frozenset({USER_GROUP}),
            administrator_groups=frozenset({ADMIN_GROUP}),
        ),
        kerberos_profile_resolver=_profile,
        kerberos_group_resolver=lambda principal: memberships.get(principal, set()),
        answer_workers=1,
    )


def _seed_completed_owner_exports(bench, matter, owner_id):
    # These synthetic jobs are claimed and completed by the fixture itself.
    if bench.research is not None:
        bench.research.close()
    if bench.full_review is not None:
        bench.full_review.close()
    document, _created = bench.source_store(matter).store_stream(
        "generated-authority-source.txt",
        "text/plain",
        io.BytesIO(b"Generated source used only for an authorization regression."),
    )
    notebook_item, _created = bench.workspace.create_notebook_item(
        matter.matter_id,
        owner_id,
        item_type="note",
        status="confirmed",
        title="Generated owner notebook entry",
        body="Synthetic notebook work product.",
    )

    research, _created = bench.workspace.queue_research_job(
        matter.matter_id,
        owner_id,
        "What does the generated source establish?",
        "Generated completed investigation",
        "research-request-" + "e" * 32,
    )
    claimed_research = bench.workspace.claim_research_job(
        "generated-export-research-worker"
    )
    assert claimed_research is not None
    assert claimed_research.job_id == research.job_id
    research = bench.workspace.finish_research_job(
        research.job_id,
        {
            "summary": "No supported finding was retained.",
            "passes": [
                {
                    "query": "generated source",
                    "status": "gap",
                    "text": "No searchable passage matched this part of the research plan.",
                }
            ],
            "evidence": [],
            "coverage": {
                "search_pass_count": 1,
                "candidate_passage_count": 1,
                "evidence_passage_count": 0,
                "evidence_source_count": 0,
                "scope": "matter",
                "notice": "Synthetic authorization fixture.",
            },
            "answer": {
                "answerable": False,
                "introduction": "",
                "claims": [],
                "limitation": None,
                "missing_information": "No supported finding was retained.",
            },
        },
    )
    assert research.state == "succeeded"

    _criterion, version = bench.workspace.create_review_criterion(
        matter.matter_id,
        owner_id,
        title="Generated every-source criterion",
        instructions="Include sources containing a generated matching term.",
    )
    review = bench.workspace.queue_review_run(
        matter.matter_id,
        owner_id,
        version.criterion_version_id,
        run_kind="full",
    )
    claimed_review = bench.workspace.claim_review_run(
        "generated-export-review-worker"
    )
    assert claimed_review is not None
    assert claimed_review.run_id == review.run_id
    decisions = bench.workspace.review_decisions_for_export(
        matter.matter_id, owner_id, review.run_id
    )
    assert len(decisions) == 1
    assert decisions[0].document_id == document.document_id
    bench.workspace.record_review_decision(
        review.run_id,
        document.document_id,
        decision="excluded",
        rationale="The generated matching term is absent.",
    )
    review = bench.workspace.finish_review_run(review.run_id)
    assert review.state == "succeeded"
    return notebook_item, research, review


def _seed_cited_research(bench, matter, owner_id):
    # This export fixture claims and completes its own job. Join the automatic
    # worker before queueing so it cannot claim the fixture's job first.
    bench.research.close()
    document, _created = bench.source_store(matter).store_stream(
        "generated-frozen-source.txt",
        "text/plain",
        io.BytesIO(b"Generated frozen source says the blue vehicle arrived at noon."),
    )
    unit = document.parsed_units()[0]
    citation = bench._citation(matter, bench._candidate(matter, document, unit, 1))
    evidence = bench._workflow_citation_payload(citation)
    research, _created = bench.workspace.queue_research_job(
        matter.matter_id,
        owner_id,
        "What does the frozen source say about the blue vehicle?",
        "Generated frozen-source investigation",
        "research-request-" + "a" * 32,
    )
    claimed = bench.workspace.claim_research_job("generated-frozen-export-worker")
    assert claimed is not None and claimed.job_id == research.job_id
    return bench._finish_research_job(
        claimed,
        {
            "summary": "The blue vehicle arrived at noon.",
            "passes": [],
            "evidence": [evidence],
            "coverage": {
                "search_pass_count": 1,
                "candidate_passage_count": 1,
                "evidence_passage_count": 1,
                "evidence_source_count": 1,
                "scope": "matter",
                "notice": "Synthetic frozen-source fixture.",
            },
            "answer": {
                "answerable": True,
                "introduction": "",
                "claims": [
                    {
                        "text": "The blue vehicle arrived at noon.",
                        "citations": [evidence],
                    }
                ],
                "limitation": None,
                "missing_information": "",
            },
        },
    )


def _seed_empty_research(bench, matter, owner_id, suffix: str):
    bench.research.close()
    research, _created = bench.workspace.queue_research_job(
        matter.matter_id,
        owner_id,
        "What does the generated record establish?",
        f"Generated bounded investigation {suffix}",
        "research-request-" + suffix * 32,
    )
    claimed = bench.workspace.claim_research_job(
        f"generated-bounded-research-worker-{suffix}"
    )
    assert claimed is not None and claimed.job_id == research.job_id
    return bench.workspace.finish_research_job(
        research.job_id,
        {
            "summary": "No supported finding was retained.",
            "passes": [],
            "evidence": [],
            "coverage": {},
            "answer": {
                "answerable": False,
                "introduction": "",
                "claims": [],
                "limitation": None,
                "missing_information": "No supported finding was retained.",
            },
        },
    )


def test_store_requires_owner_unless_validated_administrator_override(tmp_path):
    store = WorkspaceStore(tmp_path / "workbench.sqlite")
    store.upsert_principal(
        "test", "owner", "Matter Owner", "owner", preferred_principal_id="principal-owner"
    )
    store.upsert_principal(
        "test", "member", "Matter Member", "member", preferred_principal_id="principal-member"
    )
    store.upsert_principal(
        "test", "admin", "App Administrator", "admin", preferred_principal_id="principal-admin"
    )
    target = store.create_matter("Synthetic target", "", "principal-owner")
    neighbor = store.create_matter("Synthetic neighbor", "", "principal-owner")
    store.add_member(target.matter_id, "principal-member", "principal-owner")
    document_id = "1" * 32
    source_version_id = "2" * 32
    store.upsert_source_catalog(
        target.matter_id,
        (
            {
                "document_id": document_id,
                "version_id": source_version_id,
                "action_token": "3" * 32,
                "display_name": "Generated cleanup source.txt",
                "relative_path": "generated/cleanup-source.txt",
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
                "byte_size": 24,
                "origin": "upload",
                "retryable": False,
                "removable": True,
                "has_video": False,
            },
        ),
    )
    store.create_report(
        target.matter_id,
        "principal-owner",
        "Generated cleanup report",
        "Synthetic purge regression",
    )
    analysis = store.start_analysis_run(target.matter_id, "principal-owner")
    store.fail_analysis_run(
        analysis.analysis_id,
        target.matter_id,
        "Generated analysis stopped safely.",
    )
    store.create_notebook_item(
        target.matter_id,
        "principal-owner",
        item_type="note",
        status="confirmed",
        title="Generated cleanup note",
        body="Synthetic work product for purge verification.",
    )
    media_job = store.queue_media_job(
        target.matter_id,
        document_id,
        source_version_id,
        "principal-owner",
        source_sha256="4" * 64,
        byte_size=24,
        media_type="audio/wav",
        duration_ms=2_000,
    )
    claimed_media = store.claim_media_job("synthetic-cleanup-worker")
    assert claimed_media is not None and claimed_media.media_job_id == media_job.media_job_id
    store.import_media_transcript(
        media_job.media_job_id,
        segments=(
            {
                "external_segment_id": "generated-segment-1",
                "start_ms": 0,
                "end_ms": 1_000,
                "speaker_cluster": "SPEAKER_00",
                "model_text": "Generated transcript text for cleanup.",
                "confidence": 0.95,
            },
        ),
        warnings=(),
        quality={},
        provenance={},
    )
    store.finish_media_job(
        media_job.media_job_id,
        degraded=False,
        message="Generated transcript ready.",
    )
    summary = store.media_summary(target.matter_id, document_id, source_version_id)
    assert summary is not None
    store.start_media_summary(summary.transcript_id)
    store.fail_media_summary(
        summary.transcript_id,
        "Generated optional overview stopped safely.",
    )

    with pytest.raises(KeyError):
        store.matter_for_closure(target.slug, "principal-member")
    with pytest.raises(KeyError):
        store.case_lifecycle.begin_exit(target.slug, "principal-admin", session_id=lifecycle_session(store, "principal-admin", administrator_override=False).session_id)
    confirmation = prepare_purge_confirmation(store, target, "principal-admin", administrator_override=True)
    prepared, lifecycle = store.begin_matter_purge(
        target.slug, "principal-admin", target.display_name, source_count=0,
        administrator_override=True, **confirmation,
    )
    assert prepared.matter_id == target.matter_id
    assert lifecycle.requested_by == "principal-admin"
    store.complete_matter_purge(target.matter_id, lifecycle.purge_id or "")
    preserved_tables = {
        "workbench_audit_event",
        "workbench_matter",
        "workbench_matter_lifecycle",
        "workbench_matter_retention",
        "workbench_case_policy",
        "workbench_case_exit",
    }
    remaining: dict[str, int] = {}
    table_rows = store.connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'workbench_%'"
    ).fetchall()
    for table_row in table_rows:
        table = str(table_row[0])
        columns = {
            str(column[1])
            for column in store.connection.execute("SELECT * FROM pragma_table_info(?)", (table,))
        }
        if "matter_id" not in columns or table in preserved_tables:
            continue
        remaining[table] = int(
            store.connection.execute(
                f'SELECT COUNT(*) FROM "{table}" WHERE matter_id=?',
                (target.matter_id,),
            ).fetchone()[0]
        )
    assert remaining
    not_empty = {table: count for table, count in remaining.items() if count}
    assert not not_empty, not_empty
    assert store.get_active_matter(neighbor.slug) == neighbor
    with fixture_write(store):
        store.connection.execute(
            "UPDATE workbench_principal SET active=0 WHERE principal_id=?",
            ("principal-admin",),
        )
    with pytest.raises(WorkspaceProblem, match="administrator identity is not active"):
        store.matter_for_closure(
            neighbor.slug,
            "principal-admin",
            administrator_override=True,
        )
    store.close()


def test_purge_refuses_every_durable_media_and_analysis_queue(tmp_path):
    store = WorkspaceStore(tmp_path / "workbench.sqlite")
    owner = store.upsert_principal(
        "test", "owner", "Matter Owner", "owner", preferred_principal_id="principal-owner"
    )
    matter = store.create_matter("Generated active-work matter", "", owner.principal_id)
    document_id = "1" * 32
    source_version_id = "2" * 32
    store.upsert_source_catalog(
        matter.matter_id,
        (
            {
                "document_id": document_id,
                "version_id": source_version_id,
                "action_token": "3" * 32,
                "display_name": "Generated recording.wav",
                "relative_path": "generated/recording.wav",
                "media_type": "audio/wav",
                "kind": "AUDIO",
                "source_state": "ready",
                "tone": "ready",
                "state_label": "Searchable",
                "count_label": "2 seconds",
                "processing_stage": "",
                "completed_units": 1,
                "total_units": 1,
                "page_count": 0,
                "duration_ms": 2_000,
                "byte_size": 24,
                "origin": "upload",
                "retryable": False,
                "removable": True,
                "has_video": False,
            },
        ),
    )
    media_job = store.queue_media_job(
        matter.matter_id,
        document_id,
        source_version_id,
        owner.principal_id,
        source_sha256="4" * 64,
        byte_size=24,
        media_type="audio/wav",
        duration_ms=2_000,
    )
    assert store.active_matter_work_counts(matter.matter_id)["media"] == 1
    with pytest.raises(WorkspaceProblem, match="media processing"):
        store.case_lifecycle.begin_exit(matter.slug, owner.principal_id, session_id=lifecycle_session(store, owner.principal_id, administrator_override=False).session_id)

    claimed = store.claim_media_job("generated-media-worker")
    assert claimed is not None and claimed.media_job_id == media_job.media_job_id
    store.import_media_transcript(
        media_job.media_job_id,
        segments=(
            {
                "external_segment_id": "generated-segment-1",
                "start_ms": 0,
                "end_ms": 1_000,
                "speaker_cluster": "SPEAKER_00",
                "model_text": "Generated transcript text.",
                "confidence": 0.9,
            },
        ),
        warnings=(),
        quality={},
        provenance={},
    )
    store.finish_media_job(
        media_job.media_job_id,
        degraded=False,
        message="Generated transcript ready.",
    )
    summary = store.media_summary(matter.matter_id, document_id, source_version_id)
    assert summary is not None
    assert store.active_matter_work_counts(matter.matter_id)["overviews"] == 1
    with pytest.raises(WorkspaceProblem, match="transcript overviews"):
        store.case_lifecycle.begin_exit(matter.slug, owner.principal_id, session_id=lifecycle_session(store, owner.principal_id, administrator_override=False).session_id)
    with fixture_write(store):
        store.connection.execute(
            "UPDATE workbench_media_summary SET state='stale' WHERE transcript_id=?",
            (summary.transcript_id,),
        )
    assert store.active_matter_work_counts(matter.matter_id)["overviews"] == 1
    store.queue_media_summary(
        matter.matter_id, document_id, source_version_id, owner.principal_id
    )
    store.start_media_summary(summary.transcript_id)
    store.fail_media_summary(summary.transcript_id, "Generated overview stopped safely.")

    analysis = store.start_analysis_run(matter.matter_id, owner.principal_id)
    assert store.active_matter_work_counts(matter.matter_id)["analysis"] == 1
    with pytest.raises(WorkspaceProblem, match="analysis"):
        store.case_lifecycle.begin_exit(matter.slug, owner.principal_id, session_id=lifecycle_session(store, owner.principal_id, administrator_override=False).session_id)
    store.fail_analysis_run(
        analysis.analysis_id, matter.matter_id, "Generated analysis stopped safely."
    )
    assert not any(store.active_matter_work_counts(matter.matter_id).values())
    store.close()


def test_restart_recovers_abandoned_analysis_and_unblocks_matter_purge(tmp_path):
    runtime = tmp_path / "runtime"
    first = CaseIntelligenceWorkbench(runtime, answer_workers=1)
    owner = first.workspace.upsert_principal(
        "preview",
        "taylor-morgan",
        "Taylor Morgan",
        "taylor.morgan@example.test",
        preferred_principal_id="development-taylor-morgan",
    )
    matter = first.create_matter(
        "Generated interrupted analysis matter",
        "Synthetic restart recovery fixture",
        owner.principal_id,
    )
    completed = first.workspace.start_analysis_run(
        matter.matter_id, owner.principal_id
    )
    first.workspace.complete_analysis_run(
        completed.analysis_id,
        matter.matter_id,
        owner.principal_id,
        source_count=0,
        unit_count=0,
        entity_count=0,
        capped=False,
        findings=(),
    )
    failed = first.workspace.start_analysis_run(matter.matter_id, owner.principal_id)
    first.workspace.fail_analysis_run(
        failed.analysis_id,
        matter.matter_id,
        "Generated prior analysis failure.",
    )
    abandoned = first.workspace.start_analysis_run(
        matter.matter_id, owner.principal_id
    )
    terminal_before = {
        row["analysis_id"]: (row["state"], row["message"], row["finished_at"])
        for row in first.workspace.connection.execute(
            "SELECT analysis_id,state,message,finished_at "
            "FROM workbench_analysis_run WHERE analysis_id IN (?,?)",
            (completed.analysis_id, failed.analysis_id),
        ).fetchall()
    }
    assert first.workspace.active_matter_work_counts(matter.matter_id)["analysis"] == 1
    first.close()

    app = create_workbench_app(
        runtime,
        generator=UnavailableGenerator(),
        auth_mode="test",
    )
    with TestClient(app) as client:
        bench = client.app.state.workbench
        recovered = bench.workspace.latest_analysis_run(matter.matter_id)
        assert recovered is not None
        assert recovered.analysis_id == abandoned.analysis_id
        assert recovered.state == "failed"
        assert recovered.finished_at is not None
        assert recovered.message == (
            "The review map refresh was interrupted by an application restart. "
            "Select Refresh review map to retry. Existing review decisions are unchanged."
        )
        terminal_after = {
            row["analysis_id"]: (row["state"], row["message"], row["finished_at"])
            for row in bench.workspace.connection.execute(
                "SELECT analysis_id,state,message,finished_at "
                "FROM workbench_analysis_run WHERE analysis_id IN (?,?)",
                (completed.analysis_id, failed.analysis_id),
            ).fetchall()
        }
        assert terminal_after == terminal_before
        assert bench.workspace.recover_running_analysis_runs() == 0
        assert bench.workspace.active_matter_work_counts(matter.matter_id)["analysis"] == 0

        page = client.get(f"/matters/{matter.slug}/analysis")
        assert page.status_code == 200
        assert recovered.message in page.text
        assert "Refresh review map" in page.text

        confirmation = prepare_bench_exit(bench, matter, owner.principal_id)
        prepared, lifecycle = bench.begin_matter_purge(
            matter.slug, owner.principal_id, matter.display_name, **confirmation,
        )
        assert prepared.matter_id == matter.matter_id
        assert lifecycle.state == "purging"


def test_playback_admission_cannot_cross_the_transactional_purge_claim(
    tmp_path, monkeypatch
):
    bench = CaseIntelligenceWorkbench(tmp_path / "runtime", answer_workers=1)
    owner = bench.workspace.upsert_principal(
        "test", "owner", "Matter Owner", "owner", preferred_principal_id="principal-owner"
    )
    matter = bench.create_matter(
        "Generated playback boundary", "Synthetic fixture", owner.principal_id
    )
    store = bench.source_store(matter)
    document, _ = store.store_stream(
        "generated-source.txt",
        "text/plain",
        io.BytesIO(b"Generated source for a queue boundary."),
    )
    assert bench.playback is not None
    entered_claim = threading.Event()
    release_claim = threading.Event()
    playback_finished = threading.Event()
    confirmation = prepare_bench_exit(bench, matter, owner.principal_id)
    real_begin = bench.workspace.case_lifecycle.claim_purge
    outcomes: dict[str, object] = {}

    def paused_begin(*args, **kwargs):
        entered_claim.set()
        assert release_claim.wait(timeout=3)
        return real_begin(*args, **kwargs)

    monkeypatch.setattr(bench.workspace.case_lifecycle, "claim_purge", paused_begin)

    def purge():
        outcomes["purge"] = bench.begin_matter_purge(
            matter.slug, owner.principal_id, matter.display_name, **confirmation,
        )

    def start_playback():
        try:
            bench.playback.ensure(matter, document)
        except Exception as exc:  # captured for deterministic cross-thread assertion
            outcomes["playback"] = exc
        finally:
            playback_finished.set()

    purge_thread = threading.Thread(target=purge)
    purge_thread.start()
    assert entered_claim.wait(timeout=3)
    playback_thread = threading.Thread(target=start_playback)
    playback_thread.start()
    assert not playback_finished.wait(timeout=0.1)
    release_claim.set()
    purge_thread.join(timeout=3)
    playback_thread.join(timeout=3)

    assert "purge" in outcomes
    assert isinstance(outcomes.get("playback"), UploadProblem)
    assert "cannot open its source storage" in str(outcomes["playback"])
    assert bench.workspace.matter_lifecycle(matter.matter_id).state == "purging"
    assert not bench.playback.has_matter_work(matter.matter_id)
    bench.close()


def test_owner_can_find_manage_matters_and_matter_settings(tmp_path):
    with TestClient(_app(tmp_path), base_url=f"https://{LEGACY_STEM}.example.test") as client:
        assert client.get(
            "/auth/login", headers=_headers(OWNER), follow_redirects=False
        ).status_code == 303
        landing = client.get("/matters/new", headers=_headers(OWNER))
        slug = _create_matter(
            client,
            principal=OWNER,
            csrf_token=_csrf(landing.text),
            name="Synthetic managed matter",
        )

        home = client.get(f"/matters/{slug}/home", headers=_headers(OWNER))
        assert home.status_code == 200
        assert 'href="/matters/manage"' in home.text
        assert f'href="/matters/{slug}/settings"' in home.text

        managed = client.get("/matters/manage", headers=_headers(OWNER))
        assert managed.status_code == 200
        assert "Manage matters" in managed.text
        assert "Synthetic managed matter" in managed.text
        assert "You own this matter" in managed.text

        settings = client.get(f"/matters/{slug}/settings", headers=_headers(OWNER))
        assert settings.status_code == 200
        assert "Matter settings" in settings.text
        assert "Download work-product bundle" in settings.text
        assert "excludes original source files" in settings.text
        assert f'href="/matters/{slug}/close"' in settings.text
        assert "Close matter" in settings.text

        assert client.get(
            "/matters/m-000000000000/settings", headers=_headers(OWNER)
        ).status_code == 404

        client.cookies.clear()
        assert client.get(
            "/auth/login", headers=_headers(OTHER), follow_redirects=False
        ).status_code == 303
        isolated = client.get("/matters/manage", headers=_headers(OTHER))
        assert isolated.status_code == 200
        assert "Synthetic managed matter" not in isolated.text
        assert client.get(
            f"/matters/{slug}/settings", headers=_headers(OTHER)
        ).status_code == 404
        assert client.get(
            f"/matters/{slug}/close", headers=_headers(OTHER)
        ).status_code == 404


def test_administrator_close_is_explicit_safe_attributed_and_matter_isolated(
    tmp_path, monkeypatch
):
    external_original = tmp_path / "external-original.txt"
    external_original.write_text("Generated source retained outside the workbench.")
    runtime = tmp_path / "runtime"
    with TestClient(_app(tmp_path), base_url=f"https://{LEGACY_STEM}.example.test") as client:
        assert client.get(
            "/auth/login", headers=_headers(OWNER), follow_redirects=False
        ).status_code == 303
        owner_landing = client.get("/matters/new", headers=_headers(OWNER))
        owner_csrf = _csrf(owner_landing.text)
        target_slug = _create_matter(
            client,
            principal=OWNER,
            csrf_token=owner_csrf,
            name="Synthetic administrator deletion target",
        )
        neighbor_slug = _create_matter(
            client,
            principal=OWNER,
            csrf_token=owner_csrf,
            name="Synthetic isolation neighbor",
        )
        bench = client.app.state.workbench
        target = bench.matter(target_slug, _principal_id(client, OWNER))
        neighbor = bench.matter(neighbor_slug, target.owner_id)
        active_upload, _ = bench.workspace.create_upload_session(
            target.matter_id,
            target.owner_id,
            "Generated active upload",
            (
                {
                    "display_name": "generated-pending.txt",
                    "relative_path": "generated-pending.txt",
                    "media_type": "text/plain",
                    "expected_size": 32,
                },
            ),
        )

        client.cookies.clear()
        assert client.get(
            "/auth/login", headers=_headers(ADMIN), follow_redirects=False
        ).status_code == 303
        managed = client.get("/matters/manage", headers=_headers(ADMIN))
        admin_csrf = _csrf(managed.text)
        assert "Manage matters" in managed.text
        assert "Administrator oversight" in managed.text
        assert f'href="/matters/{target_slug}/settings"' in managed.text

        settings = client.get(
            f"/matters/{target_slug}/settings", headers=_headers(ADMIN)
        )
        assert settings.status_code == 200
        assert "Administrator authority" in settings.text
        assert "Matter Owner" in settings.text
        assert "Close matter" in settings.text

        close_page = client.get(
            f"/matters/{target_slug}/close", headers=_headers(ADMIN)
        )
        assert close_page.status_code == 200
        assert "Administrator action" in close_page.text
        assert "Matter Owner" in close_page.text
        assert "attributed to your account" in close_page.text
        assert "Export and exit." in close_page.text
        assert "External registered originals are not deleted" in close_page.text
        assert re.search(r'<button[^>]+type="submit"[^>]+disabled', close_page.text)

        refused = client.post(
            f"/matters/{target_slug}/close",
            data={
                "csrf_token": admin_csrf,
                "confirmed_name": target.display_name,
                "acknowledge": "yes",
            },
            headers=_headers(ADMIN),
            follow_redirects=False,
        )
        assert refused.status_code == 303
        assert bench.workspace.matter_lifecycle(target.matter_id).state == "active"
        bench.workspace.cancel_upload_session(
            target.matter_id,
            target.owner_id,
            active_upload.upload_session_id,
        )

        assert bench.playback is not None
        monkeypatch.setattr(
            bench.playback,
            "has_matter_work",
            lambda matter_id: matter_id == target.matter_id,
        )
        playback_page = client.get(
            f"/matters/{target_slug}/close", headers=_headers(ADMIN)
        )
        assert bench.matter_active_work_counts(target.matter_id)["playback"] == 1
        assert "Wait for active processing and downloads" in playback_page.text
        assert re.search(r'<button[^>]+type="submit"[^>]+disabled', playback_page.text)
        playback_refused = client.post(
            f"/matters/{target_slug}/close",
            data={
                "csrf_token": admin_csrf,
                "confirmed_name": target.display_name,
                "acknowledge": "yes",
            },
            headers=_headers(ADMIN),
            follow_redirects=False,
        )
        assert playback_refused.status_code == 303
        assert bench.workspace.matter_lifecycle(target.matter_id).state == "active"
        monkeypatch.setattr(bench.playback, "has_matter_work", lambda _matter_id: False)

        missing_acknowledgement = client.post(
            f"/matters/{target_slug}/close",
            data={
                "csrf_token": admin_csrf,
                "confirmed_name": target.display_name,
            },
            headers=_headers(ADMIN),
            follow_redirects=False,
        )
        assert missing_acknowledgement.status_code == 303
        assert bench.workspace.matter_lifecycle(target.matter_id).state == "active"

        wrong_name = client.post(
            f"/matters/{target_slug}/close",
            data={
                "csrf_token": admin_csrf,
                "confirmed_name": "Synthetic wrong name",
                "acknowledge": "yes",
            },
            headers=_headers(ADMIN),
            follow_redirects=False,
        )
        assert wrong_name.status_code == 303
        assert bench.workspace.matter_lifecycle(target.matter_id).state == "active"

        exported = client.get(
            f"/matters/{target_slug}/export", headers=_headers(ADMIN)
        )
        assert exported.status_code == 200

        confirmation = prepare_kerberos_exit(client, target, ADMIN, _headers)
        deleted = client.post(
            f"/matters/{target_slug}/close", data=confirmation,
            headers=_headers(ADMIN),
            follow_redirects=False,
        )
        assert deleted.status_code == 303
        assert bench.workspace.matter_lifecycle(target.matter_id).state == "deleted"
        assert not (runtime / "matters" / target.matter_id).exists()
        assert external_original.read_text() == "Generated source retained outside the workbench."
        assert bench.workspace.get_active_matter(neighbor_slug) == neighbor
        assert client.get(
            f"/matters/{neighbor_slug}/home", headers=_headers(ADMIN)
        ).status_code == 200
        assert client.get(
            f"/matters/{target_slug}/home", headers=_headers(ADMIN)
        ).status_code == 404

        admin_principal = bench.workspace.get_principal(_principal_id(client, ADMIN))
        lifecycle = bench.workspace.matter_lifecycle(target.matter_id)
        assert lifecycle.requested_by == admin_principal.principal_id
        purge_events = [
            event
            for event in bench.workspace.audit_events(target.matter_id)
            if event.action in {"matter.purge_confirm", "matter.purge"}
        ]
        assert purge_events
        assert all(
            event.actor_principal_id == admin_principal.principal_id
            for event in purge_events
        )
        for event in purge_events:
            assert event.details.get("role") == "administrator"
            assert target.display_name not in json.dumps(event.details)


def test_administrator_exports_closed_matter_after_owner_deactivation_with_attribution(
    tmp_path, monkeypatch
):
    with TestClient(_app(tmp_path), base_url=f"https://{LEGACY_STEM}.example.test") as client:
        assert client.get(
            "/auth/login", headers=_headers(OWNER), follow_redirects=False
        ).status_code == 303
        owner_landing = client.get("/matters/new", headers=_headers(OWNER))
        owner_csrf = _csrf(owner_landing.text)
        slug = _create_matter(
            client,
            principal=OWNER,
            csrf_token=owner_csrf,
            name="Synthetic closed export matter",
        )
        bench = client.app.state.workbench
        owner_id = _principal_id(client, OWNER)
        matter = bench.matter(slug, owner_id)
        conversation = bench.workspace.get_conversation(matter.matter_id)
        bench.workspace.append_message(
            matter.matter_id,
            conversation.conversation_id,
            "user",
            "Generated note retained for the final work-product bundle.",
        )

        def fail_clip_cleanup(_matter_id: str) -> None:
            raise RuntimeError("synthetic pre-purge cleanup failure")

        monkeypatch.setattr(bench, "_cleanup_matter_clip_exports", fail_clip_cleanup)
        confirmation = prepare_kerberos_exit(client, matter, OWNER, _headers)
        interrupted = client.post(
            f"/matters/{slug}/close",
            data=confirmation,
            headers=_headers(OWNER),
            follow_redirects=False,
        )
        assert interrupted.status_code == 303
        assert bench.workspace.matter_lifecycle(matter.matter_id).state == "purge_failed"

        with fixture_write(bench.workspace):
            bench.workspace.connection.execute(
                "UPDATE workbench_principal SET active=0 WHERE principal_id=?",
                (owner_id,),
            )
        assert not bench.workspace.get_principal(owner_id).active

        client.cookies.clear()
        assert client.get(
            "/auth/login", headers=_headers(ADMIN), follow_redirects=False
        ).status_code == 303
        exported = client.get(f"/matters/{slug}/export", headers=_headers(ADMIN))
        assert exported.status_code == 200, exported.text
        with zipfile.ZipFile(io.BytesIO(exported.content)) as archive:
            assert archive.testzip() is None
            conversations = json.loads(archive.read("conversations.json"))
        assert conversations["conversations"][0]["messages"][0]["content"] == (
            "Generated note retained for the final work-product bundle."
        )

        admin_id = _principal_id(client, ADMIN)
        export_events = [
            event
            for event in bench.workspace.audit_events(matter.matter_id)
            if event.action == "work_product.export"
            and event.object_type == "matter"
        ]
        assert export_events
        assert export_events[-1].actor_principal_id == admin_id
        assert export_events[-1].actor_principal_id != owner_id
        assert export_events[-1].details.get("role") == "administrator"


def test_failed_deletion_remains_discoverable_and_exportable_after_restart(
    tmp_path, monkeypatch
):
    slug = ""
    matter_id = ""
    with TestClient(_app(tmp_path), base_url=f"https://{LEGACY_STEM}.example.test") as client:
        assert client.get(
            "/auth/login", headers=_headers(OWNER), follow_redirects=False
        ).status_code == 303
        landing = client.get("/matters/new", headers=_headers(OWNER))
        owner_csrf = _csrf(landing.text)
        slug = _create_matter(
            client,
            principal=OWNER,
            csrf_token=owner_csrf,
            name="Synthetic deletion recovery matter",
        )
        bench = client.app.state.workbench
        owner_id = _principal_id(client, OWNER)
        matter = bench.matter(slug, owner_id)
        matter_id = matter.matter_id
        conversation = bench.workspace.get_conversation(matter.matter_id)
        bench.workspace.append_message(
            matter.matter_id,
            conversation.conversation_id,
            "user",
            "Generated work product retained for recovery.",
        )

        def fail_clip_cleanup(_matter_id: str) -> None:
            raise RuntimeError("synthetic deletion recovery failure")

        monkeypatch.setattr(bench, "_cleanup_matter_clip_exports", fail_clip_cleanup)
        confirmation = prepare_kerberos_exit(client, matter, OWNER, _headers)
        failed = client.post(
            f"/matters/{slug}/close",
            data=confirmation,
            headers=_headers(OWNER),
            follow_redirects=False,
        )
        assert failed.status_code == 303
        assert bench.workspace.matter_lifecycle(matter_id).state == "purge_failed"

        owner_manage = client.get("/matters/manage", headers=_headers(OWNER))
        assert owner_manage.status_code == 200
        assert "Previous deletion did not finish" in owner_manage.text
        assert f'/matters/{slug}/close' in owner_manage.text
        owner_close = client.get(f"/matters/{slug}/close", headers=_headers(OWNER))
        assert owner_close.status_code == 200
        assert "Download full export" in owner_close.text
        assert "confirmed deletion did not finish" in owner_close.text
        assert client.get(
            f"/matters/{slug}/export", headers=_headers(OWNER)
        ).status_code == 200

    with TestClient(_app(tmp_path), base_url=f"https://{LEGACY_STEM}.example.test") as client:
        assert client.get(
            "/auth/login", headers=_headers(OWNER), follow_redirects=False
        ).status_code == 303
        owner_manage = client.get("/matters/manage", headers=_headers(OWNER))
        assert "Synthetic deletion recovery matter" in owner_manage.text
        assert "Review closure" in owner_manage.text
        owner_bundle = client.get(f"/matters/{slug}/export", headers=_headers(OWNER))
        assert owner_bundle.status_code == 200
        with zipfile.ZipFile(io.BytesIO(owner_bundle.content)) as archive:
            assert archive.testzip() is None
            assert "Generated work product retained for recovery." in archive.read(
                "conversations.json"
            ).decode()

        client.cookies.clear()
        assert client.get(
            "/auth/login", headers=_headers(ADMIN), follow_redirects=False
        ).status_code == 303
        admin_manage = client.get("/matters/manage", headers=_headers(ADMIN))
        assert admin_manage.status_code == 200
        assert "Previous deletion did not finish" in admin_manage.text
        assert f'/matters/{slug}/close' in admin_manage.text
        admin_console = client.get("/admin", headers=_headers(ADMIN))
        assert admin_console.status_code == 200
        assert "Failed deletions" in admin_console.text
        assert "Synthetic deletion recovery matter" in admin_console.text
        admin_close = client.get(f"/matters/{slug}/close", headers=_headers(ADMIN))
        assert admin_close.status_code == 200
        assert "Administrator action" in admin_close.text
        assert "Download full export" in admin_close.text
        assert client.get(
            f"/matters/{slug}/export", headers=_headers(ADMIN)
        ).status_code == 200


def test_administrator_direct_exports_use_override_and_remain_matter_isolated(
    tmp_path,
):
    with TestClient(_app(tmp_path), base_url=f"https://{LEGACY_STEM}.example.test") as client:
        assert client.get(
            "/auth/login", headers=_headers(OWNER), follow_redirects=False
        ).status_code == 303
        owner_landing = client.get("/matters/new", headers=_headers(OWNER))
        owner_csrf = _csrf(owner_landing.text)
        target_slug = _create_matter(
            client,
            principal=OWNER,
            csrf_token=owner_csrf,
            name="Synthetic administrator export target",
        )
        neighbor_slug = _create_matter(
            client,
            principal=OWNER,
            csrf_token=owner_csrf,
            name="Synthetic administrator export neighbor",
        )
        bench = client.app.state.workbench
        owner_id = _principal_id(client, OWNER)
        target = bench.matter(target_slug, owner_id)
        neighbor = bench.matter(neighbor_slug, owner_id)
        if bench.research is not None:
            bench.research.close()
            bench.research = None
        if bench.full_review is not None:
            bench.full_review.close()
            bench.full_review = None
        notebook_item, research, review = _seed_completed_owner_exports(
            bench, target, owner_id
        )

        with fixture_write(bench.workspace):
            bench.workspace.connection.execute(
                "UPDATE workbench_principal SET active=0 WHERE principal_id=?",
                (owner_id,),
            )
        assert not bench.workspace.get_principal(owner_id).active

        client.cookies.clear()
        assert client.get(
            "/auth/login", headers=_headers(ADMIN), follow_redirects=False
        ).status_code == 303
        admin_id = _principal_id(client, ADMIN)

        notebook = client.get(
            f"/matters/{target_slug}/notebook/export",
            params={"format": "markdown"},
            headers=_headers(ADMIN),
        )
        assert notebook.status_code == 200
        assert "Generated owner notebook entry" in notebook.text
        notebook_item_export = client.get(
            f"/matters/{target_slug}/notebook/items/{notebook_item.item_id}/export",
            params={"format": "markdown"},
            headers=_headers(ADMIN),
        )
        assert notebook_item_export.status_code == 200
        assert "Synthetic notebook work product" in notebook_item_export.text

        investigation = client.get(
            f"/matters/{target_slug}/research/{research.job_id}/export",
            params={"format": "json"},
            headers=_headers(ADMIN),
        )
        assert investigation.status_code == 200, investigation.text
        investigation_payload = investigation.json()
        assert investigation_payload["investigation"]["synthesis"] == (
            "No supported finding was retained."
        )
        source_check = client.get(
            f"/matters/{target_slug}/full-review/{review.run_id}/export",
            params={"format": "json"},
            headers=_headers(ADMIN),
        )
        assert source_check.status_code == 200, source_check.text
        source_check_payload = source_check.json()
        assert source_check_payload["check"]["status"] == "Succeeded"
        assert source_check_payload["decisions"][0]["source_name"] == (
            "generated-authority-source.txt"
        )

        neighbor_notebook = client.get(
            f"/matters/{neighbor_slug}/notebook/export",
            params={"format": "markdown"},
            headers=_headers(ADMIN),
        )
        assert neighbor_notebook.status_code == 200
        assert "Generated owner notebook entry" not in neighbor_notebook.text
        assert client.get(
            f"/matters/{neighbor_slug}/notebook/items/{notebook_item.item_id}/export",
            params={"format": "markdown"},
            headers=_headers(ADMIN),
        ).status_code == 404
        assert client.get(
            f"/matters/{neighbor_slug}/research/{research.job_id}/export",
            params={"format": "json"},
            headers=_headers(ADMIN),
        ).status_code == 404
        assert client.get(
            f"/matters/{neighbor_slug}/full-review/{review.run_id}/export",
            params={"format": "json"},
            headers=_headers(ADMIN),
        ).status_code == 404

        successful_exports = [
            event
            for event in bench.workspace.audit_events(target.matter_id)
            if event.action
            in {"work_product.export", "research.export", "full_review.export"}
            and event.outcome == "success"
        ]
        assert len(successful_exports) == 4
        assert all(event.actor_principal_id == admin_id for event in successful_exports)
        assert all(event.actor_principal_id != owner_id for event in successful_exports)
        assert bench.matter_active_work_counts(target.matter_id)["exports"] == 0
        assert bench.matter_active_work_counts(neighbor.matter_id)["exports"] == 0


def test_oversized_final_bundle_returns_409_and_releases_response_lease(
    tmp_path, monkeypatch
):
    with TestClient(_app(tmp_path), base_url=f"https://{LEGACY_STEM}.example.test") as client:
        assert client.get(
            "/auth/login", headers=_headers(OWNER), follow_redirects=False
        ).status_code == 303
        owner_landing = client.get("/matters/new", headers=_headers(OWNER))
        slug = _create_matter(
            client,
            principal=OWNER,
            csrf_token=_csrf(owner_landing.text),
            name="Synthetic bounded bundle matter",
        )
        bench = client.app.state.workbench
        owner_id = _principal_id(client, OWNER)
        matter = bench.matter(slug, owner_id)

        monkeypatch.setattr(export_module, "MAX_BUNDLE_UNCOMPRESSED_BYTES", 1)
        refused = client.get(f"/matters/{slug}/export", headers=_headers(OWNER))
        assert refused.status_code == 409
        assert "too large to prepare at once" in refused.text
        assert bench.matter_active_work_counts(matter.matter_id)["exports"] == 0
        assert not any(
            event.action == "work_product.export" and event.outcome == "success"
            for event in bench.workspace.audit_events(matter.matter_id)
        )

        confirmation = prepare_bench_exit(bench, matter, owner_id)
        prepared, lifecycle = bench.begin_matter_purge(
            slug, owner_id, matter.display_name, **confirmation,
        )
        completed = bench.execute_matter_purge(prepared, lifecycle)
        assert completed.state == "deleted"


def test_final_bundle_keeps_cited_research_exportable_after_projection_purge_failure(
    tmp_path,
):
    runtime = tmp_path / "runtime"
    with TestClient(_app(tmp_path), base_url=f"https://{LEGACY_STEM}.example.test") as client:
        assert client.get(
            "/auth/login", headers=_headers(OWNER), follow_redirects=False
        ).status_code == 303
        landing = client.get("/matters/new", headers=_headers(OWNER))
        csrf_token = _csrf(landing.text)
        slug = _create_matter(
            client,
            principal=OWNER,
            csrf_token=csrf_token,
            name="Synthetic quarantined investigation matter",
        )
        bench = client.app.state.workbench
        owner_id = _principal_id(client, OWNER)
        matter = bench.matter(slug, owner_id)
        research = _seed_cited_research(bench, matter, owner_id)

        bench._postgres_projection_configured = True
        bench.postgres_connection = None
        bench.postgres_ready = False
        confirmation = prepare_kerberos_exit(client, matter, OWNER, _headers)
        failed = client.post(
            f"/matters/{slug}/close",
            data=confirmation,
            headers=_headers(OWNER),
            follow_redirects=False,
        )
        assert failed.status_code == 303
        lifecycle = bench.workspace.matter_lifecycle(matter.matter_id)
        assert lifecycle.state == "purge_failed"
        assert lifecycle.error_code == "projection"
        source_root = runtime / "matters" / matter.matter_id / "sources"
        quarantine = runtime / ".matter-purging" / str(lifecycle.purge_id)
        assert not source_root.exists()
        assert quarantine.is_dir()
        assert matter.matter_id not in bench._stores

        bundle = client.get(f"/matters/{slug}/export", headers=_headers(OWNER))
        assert bundle.status_code == 200, bundle.text
        with zipfile.ZipFile(io.BytesIO(bundle.content)) as archive:
            investigation_names = [
                name
                for name in archive.namelist()
                if name.startswith("investigations/") and name.endswith(".json")
            ]
            assert len(investigation_names) == 1
            exported = json.loads(archive.read(investigation_names[0]))
        assert exported["investigation"]["title"] == research.title
        assert exported["investigation"]["supporting_sources"][0]["source"] == (
            "generated-frozen-source.txt"
        )
        assert not source_root.exists()
        assert quarantine.is_dir()
        assert matter.matter_id not in bench._stores
        assert bench.matter_active_work_counts(matter.matter_id)["exports"] == 0

        successful_exports = sum(
            event.action == "work_product.export" and event.outcome == "success"
            for event in bench.workspace.audit_events(matter.matter_id)
        )
        original_result = json.loads(json.dumps(research.result))
        corrupt_result = json.loads(json.dumps(original_result))
        corrupt_result["evidence"][0]["matter_id"] = "ci-matter-" + "f" * 32
        with pytest.raises(sqlite3.IntegrityError, match="frozen"):
            with fixture_write(bench.workspace):
                bench.workspace.connection.execute(
                    "UPDATE workbench_research_job SET result_json=? WHERE job_id=?",
                    (json.dumps(corrupt_result), research.job_id),
                )
        assert bench.workspace.connection.execute(
            "SELECT result_json FROM workbench_research_job WHERE job_id=?", (research.job_id,)
        ).fetchone()[0] == json.dumps(original_result, ensure_ascii=False, separators=(",", ":"))
        assert not source_root.exists()
        assert matter.matter_id not in bench._stores
        assert successful_exports == sum(
            event.action == "work_product.export" and event.outcome == "success"
            for event in bench.workspace.audit_events(matter.matter_id)
        )

        source_root.mkdir(parents=True)
        refused_conflict = client.get(
            f"/matters/{slug}/export", headers=_headers(OWNER)
        )
        assert refused_conflict.status_code == 409
        assert "conflicting copies" in refused_conflict.text
        assert source_root.is_dir()
        assert quarantine.is_dir()
        assert matter.matter_id not in bench._stores
        source_root.rmdir()

        bench._postgres_projection_configured = False
        confirmation = prepare_kerberos_exit(client, matter, OWNER, _headers)
        retried = client.post(
            f"/matters/{slug}/close",
            data=confirmation,
            headers=_headers(OWNER),
            follow_redirects=False,
        )
        assert retried.status_code == 303
        assert bench.workspace.matter_lifecycle(matter.matter_id).state == "deleted"
        assert not quarantine.exists()


def test_final_bundle_refuses_excess_investigation_ledgers_without_omission(
    tmp_path, monkeypatch
):
    with TestClient(_app(tmp_path), base_url=f"https://{LEGACY_STEM}.example.test") as client:
        assert client.get(
            "/auth/login", headers=_headers(OWNER), follow_redirects=False
        ).status_code == 303
        landing = client.get("/matters/new", headers=_headers(OWNER))
        slug = _create_matter(
            client,
            principal=OWNER,
            csrf_token=_csrf(landing.text),
            name="Synthetic investigation ledger bound",
        )
        bench = client.app.state.workbench
        owner_id = _principal_id(client, OWNER)
        matter = bench.matter(slug, owner_id)
        _seed_empty_research(bench, matter, owner_id, "b")
        _seed_empty_research(bench, matter, owner_id, "c")
        monkeypatch.setattr(
            workbench_module, "MAX_FINAL_BUNDLE_LEDGER_ITEMS", 1, raising=False
        )

        refused = client.get(f"/matters/{slug}/export", headers=_headers(OWNER))
        assert refused.status_code == 409
        assert "more than 1 completed investigation" in refused.text
        assert "PK" not in refused.text
        assert bench.matter_active_work_counts(matter.matter_id)["exports"] == 0
        assert not any(
            event.action == "work_product.export" and event.outcome == "success"
            for event in bench.workspace.audit_events(matter.matter_id)
        )


def test_final_bundle_refuses_excess_source_check_ledgers_without_omission(
    tmp_path, monkeypatch
):
    with TestClient(_app(tmp_path), base_url=f"https://{LEGACY_STEM}.example.test") as client:
        assert client.get(
            "/auth/login", headers=_headers(OWNER), follow_redirects=False
        ).status_code == 303
        landing = client.get("/matters/new", headers=_headers(OWNER))
        slug = _create_matter(
            client,
            principal=OWNER,
            csrf_token=_csrf(landing.text),
            name="Synthetic source-check ledger bound",
        )
        bench = client.app.state.workbench
        owner_id = _principal_id(client, OWNER)
        matter = bench.matter(slug, owner_id)
        _notebook, _research, first = _seed_completed_owner_exports(
            bench, matter, owner_id
        )
        second = bench.workspace.queue_review_run(
            matter.matter_id,
            owner_id,
            first.criterion_version_id,
            run_kind="full",
        )
        claimed = bench.workspace.claim_review_run(
            "generated-second-bounded-review-worker"
        )
        assert claimed is not None and claimed.run_id == second.run_id
        for decision in bench.workspace.review_decisions_for_export(
            matter.matter_id, owner_id, second.run_id
        ):
            bench.workspace.record_review_decision(
                second.run_id,
                decision.document_id,
                decision="excluded",
                rationale="The generated matching term is absent.",
            )
        assert bench.workspace.finish_review_run(second.run_id).state == "succeeded"
        monkeypatch.setattr(
            workbench_module, "MAX_FINAL_BUNDLE_LEDGER_ITEMS", 1, raising=False
        )

        refused = client.get(f"/matters/{slug}/export", headers=_headers(OWNER))
        assert refused.status_code == 409
        assert "more than 1 every-source check" in refused.text
        assert "PK" not in refused.text
        assert bench.matter_active_work_counts(matter.matter_id)["exports"] == 0
        assert not any(
            event.action == "work_product.export" and event.outcome == "success"
            for event in bench.workspace.audit_events(matter.matter_id)
        )


def test_source_stream_and_bundle_hold_deletion_lease_until_response_finishes(
    tmp_path, monkeypatch
):
    with TestClient(_app(tmp_path), base_url=f"https://{LEGACY_STEM}.example.test") as client:
        assert client.get(
            "/auth/login", headers=_headers(OWNER), follow_redirects=False
        ).status_code == 303
        landing = client.get("/matters/new", headers=_headers(OWNER))
        slug = _create_matter(
            client,
            principal=OWNER,
            csrf_token=_csrf(landing.text),
            name="Synthetic response lease matter",
        )
        bench = client.app.state.workbench
        owner_id = _principal_id(client, OWNER)
        matter = bench.matter(slug, owner_id)
        store = bench.source_store(matter)
        source_bytes = b"Generated source bytes retained until response completion."
        document, _created = store.store_stream(
            "generated-response.txt", "text/plain", io.BytesIO(source_bytes)
        )
        token = store.action_token(document)
        source_path = store.source_path(document.document_id)

        oversized = client.get(
            f"/matters/{slug}/sources/{token}/content",
            headers={**_headers(OWNER), "Range": f"bytes={'9' * 5000}-"},
        )
        assert oversized.status_code == 416
        assert bench.matter_active_work_counts(matter.matter_id)["exports"] == 0

        entered_read = threading.Event()
        release_read = threading.Event()
        real_read = os.read
        source_inode = source_path.stat().st_ino
        read_paused = False

        def paused_read(descriptor: int, amount: int) -> bytes:
            nonlocal read_paused
            if not read_paused and os.fstat(descriptor).st_ino == source_inode:
                read_paused = True
                entered_read.set()
                assert release_read.wait(timeout=5)
            return real_read(descriptor, amount)

        monkeypatch.setattr(workbench_module.os, "read", paused_read)
        responses: dict[str, object] = {}

        def get_source() -> None:
            responses["source"] = client.get(
                f"/matters/{slug}/sources/{token}/content",
                headers=_headers(OWNER),
            )

        source_thread = threading.Thread(target=get_source)
        source_thread.start()
        assert entered_read.wait(timeout=5)
        with pytest.raises(WorkspaceProblem, match="downloads"):
            bench.begin_matter_purge(slug, owner_id, matter.display_name)
        assert source_path.exists()
        assert bench.workspace.matter_lifecycle(matter.matter_id).state == "active"
        release_read.set()
        source_thread.join(timeout=5)
        assert not source_thread.is_alive()
        assert responses["source"].status_code == 200
        assert responses["source"].content == source_bytes

        entered_bundle = threading.Event()
        release_bundle = threading.Event()
        real_bundle = workbench_module.export_matter_bundle

        def paused_bundle(*args, **kwargs):
            entered_bundle.set()
            assert release_bundle.wait(timeout=5)
            return real_bundle(*args, **kwargs)

        monkeypatch.setattr(workbench_module, "export_matter_bundle", paused_bundle)

        def get_bundle() -> None:
            responses["bundle"] = client.get(
                f"/matters/{slug}/export", headers=_headers(OWNER)
            )

        bundle_thread = threading.Thread(target=get_bundle)
        bundle_thread.start()
        assert entered_bundle.wait(timeout=5)
        with pytest.raises(WorkspaceProblem, match="downloads"):
            bench.begin_matter_purge(slug, owner_id, matter.display_name)
        assert source_path.exists()
        assert bench.workspace.matter_lifecycle(matter.matter_id).state == "active"
        release_bundle.set()
        bundle_thread.join(timeout=5)
        assert not bundle_thread.is_alive()
        assert responses["bundle"].status_code == 200
        with zipfile.ZipFile(io.BytesIO(responses["bundle"].content)) as archive:
            assert archive.testzip() is None

        confirmation = prepare_bench_exit(bench, matter, owner_id)
        prepared, lifecycle = bench.begin_matter_purge(
            slug, owner_id, matter.display_name, **confirmation,
        )
        with pytest.raises(WorkspaceProblem, match="closing"):
            bench.begin_matter_response(matter)
        completed = bench.execute_matter_purge(prepared, lifecycle)
        assert completed.state == "deleted"
        assert not source_path.exists()


def test_administrator_final_bundle_uses_durable_catalog_after_restart(tmp_path):
    slug = ""
    matter_id = ""
    owner_id = ""
    with TestClient(_app(tmp_path), base_url=f"https://{LEGACY_STEM}.example.test") as client:
        assert client.get(
            "/auth/login", headers=_headers(OWNER), follow_redirects=False
        ).status_code == 303
        landing = client.get("/matters/new", headers=_headers(OWNER))
        slug = _create_matter(
            client,
            principal=OWNER,
            csrf_token=_csrf(landing.text),
            name="Synthetic restart export matter",
        )
        bench = client.app.state.workbench
        owner_id = _principal_id(client, OWNER)
        matter = bench.matter(slug, owner_id)
        matter_id = matter.matter_id
        bench.source_store(matter).store_stream(
            "generated-catalog.txt",
            "text/plain",
            io.BytesIO(b"Generated durable catalog source."),
        )
        bench._cleanup_matter_clip_exports = lambda _matter_id: (_ for _ in ()).throw(
            RuntimeError("synthetic pre-storage purge failure")
        )
        confirmation = prepare_kerberos_exit(client, matter, OWNER, _headers)
        failed = client.post(
            f"/matters/{slug}/close",
            data=confirmation,
            headers=_headers(OWNER),
            follow_redirects=False,
        )
        assert failed.status_code == 303
        assert bench.workspace.matter_lifecycle(matter_id).state == "purge_failed"
        with fixture_write(bench.workspace):
            bench.workspace.connection.execute(
                "UPDATE workbench_principal SET active=0 WHERE principal_id=?",
                (owner_id,),
            )

    with TestClient(_app(tmp_path), base_url=f"https://{LEGACY_STEM}.example.test") as client:
        assert client.get(
            "/auth/login", headers=_headers(ADMIN), follow_redirects=False
        ).status_code == 303
        bench = client.app.state.workbench
        assert matter_id not in bench._stores
        exported = client.get(f"/matters/{slug}/export", headers=_headers(ADMIN))
        assert exported.status_code == 200
        assert matter_id not in bench._stores
        with zipfile.ZipFile(io.BytesIO(exported.content)) as archive:
            report = archive.read("matter-report.md").decode()
            assert "generated-catalog.txt" in report
            assert archive.testzip() is None
