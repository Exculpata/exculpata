from __future__ import annotations

import io
import json
import re
import zipfile
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from case_intelligence.generation import UnavailableGenerator
from case_intelligence.pilot_uploads import PilotStore, securely_delete_owned_tree
from case_intelligence.workbench import create_workbench_app
from case_intelligence.workspace_store import WorkspaceProblem, WorkspaceStore
from tests.case_lifecycle_helpers import claim_synthetic_exit, prepare_purge_confirmation
from tests.case_lifecycle_web_helpers import prepare_preview_exit, preview_login


FIXED = datetime(2026, 8, 27, 17, 0, tzinfo=timezone.utc)
WEB_ACTOR = "development-taylor-morgan"


def _seed(store: WorkspaceStore) -> None:
    store.upsert_principal(
        "test",
        "owner",
        "Owner User",
        "owner",
        preferred_principal_id="principal-owner",
    )


def _create_matter(client: TestClient, name: str) -> str:
    if "X-CSRF-Token" not in client.headers:
        preview_login(client)
    response = client.post(
        "/matters",
        data={"name": name, "descriptor": "Generated lifecycle fixture"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    return re.fullmatch(
        r"/matters/(m-[0-9a-f]{12})/setup", response.headers["location"]
    ).group(1)


def test_store_purge_refuses_active_work_then_scrubs_content_and_keeps_audit(tmp_path):
    store = WorkspaceStore(tmp_path / "workbench.sqlite", clock=lambda: FIXED)
    _seed(store)
    matter = store.create_matter("Synthetic purge matter", "Sensitive label", "principal-owner")
    conversation = store.get_conversation(matter.matter_id)
    job, _ = store.queue_answer_job(
        matter.matter_id,
        conversation.conversation_id,
        "principal-owner",
        "What is in the generated record?",
        "answer-request-" + "a" * 32,
    )
    store.append_audit_event(
        actor_principal_id="principal-owner",
        session_id=None,
        matter_id=matter.matter_id,
        request_id="request-generated-purge",
        action="matter.open",
        outcome="success",
    )

    with pytest.raises(WorkspaceProblem, match="Wait for current"):
        store.case_lifecycle.begin_exit(matter.slug, "principal-owner")
    store.cancel_answer_job(matter.matter_id, "principal-owner", job.job_id)
    confirmation = prepare_purge_confirmation(store, matter, "principal-owner")
    with pytest.raises(WorkspaceProblem, match="exactly"):
        store.begin_matter_purge(
            matter.slug, "principal-owner", "wrong name", source_count=2, **confirmation
        )

    prepared, lifecycle = store.begin_matter_purge(
        matter.slug,
        "principal-owner",
        matter.display_name,
        source_count=2, **confirmation,
    )
    assert prepared.matter_id == matter.matter_id
    assert lifecycle.state == "purging"
    assert lifecycle.source_count == 2
    assert lifecycle.message_count == 1
    assert store.list_matters("principal-owner") == ()
    with pytest.raises(KeyError):
        store.get_matter(matter.slug, "principal-owner")

    completed = store.complete_matter_purge(matter.matter_id, lifecycle.purge_id)
    assert completed.state == "deleted"
    assert completed.message_count == 1
    assert store.connection.execute(
        "SELECT COUNT(*) FROM workbench_conversation WHERE matter_id=?", (matter.matter_id,)
    ).fetchone()[0] == 0
    assert store.connection.execute(
        "SELECT COUNT(*) FROM workbench_answer_job WHERE matter_id=?", (matter.matter_id,)
    ).fetchone()[0] == 0
    scrubbed = store.connection.execute(
        "SELECT display_name,descriptor FROM workbench_matter WHERE matter_id=?",
        (matter.matter_id,),
    ).fetchone()
    assert tuple(scrubbed) == ("Deleted matter", "")
    events = store.audit_events(matter.matter_id)
    assert [event.action for event in events].count("matter.open") == 1
    assert {event.action for event in events} >= {
        "matter.exit_begin", "matter.exit_export", "matter.exit_confirmation", "matter.purge_confirm"
    }
    assert matter.display_name not in repr(events)
    with pytest.raises(KeyError):
        store.membership(matter.matter_id, "principal-owner")
    store.close()


def test_interrupted_purge_becomes_retryable_and_owned_tree_deletion_rejects_symlink(tmp_path):
    store = WorkspaceStore(tmp_path / "workbench.sqlite", clock=lambda: FIXED)
    _seed(store)
    matter = store.create_matter("Interrupted purge", "", "principal-owner")
    _, lifecycle = claim_synthetic_exit(store, matter, "principal-owner")
    assert store.recover_interrupted_matter_purges() == 1
    assert store.matter_lifecycle(matter.matter_id).state == "purge_failed"
    _, retried = claim_synthetic_exit(store, matter, "principal-owner")
    assert retried.purge_id == lifecycle.purge_id
    store.close()

    boundary = tmp_path / "purging"
    boundary.mkdir()
    unsafe = boundary / ("matter-purge-" + "a" * 32)
    unsafe.mkdir()
    (unsafe / "ordinary.txt").write_text("generated", encoding="utf-8")
    (unsafe / "outside-link").symlink_to(tmp_path / "outside")
    with pytest.raises(RuntimeError, match="symbolic link"):
        securely_delete_owned_tree(unsafe, allowed_parent=boundary)
    assert (unsafe / "outside-link").is_symlink()


def test_authenticated_exports_history_rename_and_owner_close_delete_end_to_end(tmp_path):
    runtime = tmp_path / "runtime"
    with TestClient(
        create_workbench_app(
            runtime,
            generator=UnavailableGenerator(),
            auth_mode="preview",
            answer_workers=1,
        )
    ) as client:
        slug = _create_matter(client, "Synthetic continuity route matter")
        other_slug = _create_matter(client, "Synthetic isolation matter")
        uploaded = client.post(
            f"/matters/{slug}/uploads",
            files=[
                (
                    "files",
                    (
                        "Generated notes.txt",
                        b"The generated loading dock event occurred at 10:12 p.m.\n",
                        "text/plain",
                    ),
                )
            ],
        )
        assert uploaded.status_code == 200
        bench = client.app.state.workbench
        matter = bench.matter(slug, WEB_ACTOR)
        conversation = bench.workspace.get_conversation(matter.matter_id)
        question = bench.workspace.append_message(
            matter.matter_id,
            conversation.conversation_id,
            "user",
            "What happened at the loading dock?",
        )
        answer = bench.workspace.append_message(
            matter.matter_id,
            conversation.conversation_id,
            "assistant",
            "The generated record identifies one event.",
            {
                "kind": "generated",
                "introduction": "The generated record identifies one event.",
                "claims": [
                    {
                        "text": "The event occurred at 10:12 p.m.",
                        "citations": [
                            {
                                "source_name": "Generated notes.txt",
                                "location": "Line 1",
                                "href": f"/matters/{slug}?support=" + "a" * 40,
                            }
                        ],
                    }
                ],
                "limitation": None,
            },
        )

        renamed = client.post(
            f"/matters/{slug}/conversations/{conversation.conversation_id}/rename",
            data={"title": "Loading dock timeline"},
            follow_redirects=False,
        )
        assert renamed.status_code == 303
        page = client.get(f"/matters/{slug}")
        assert "Saved conversations" in page.text
        assert "Loading dock timeline" in page.text
        assert "2 messages" in page.text
        assert "Answer ready" in page.text
        assert "Save to report" in page.text
        assert "Continue reviewing" in page.text

        answer_export = client.get(
            f"/matters/{slug}/conversations/{conversation.conversation_id}/messages/{answer.message_id}/export",
            params={"format": "markdown"},
        )
        assert answer_export.status_code == 200
        assert answer_export.headers["content-type"].startswith("text/markdown")
        assert "10:12 p.m." in answer_export.text
        assert "/matters/" not in answer_export.text

        conversation_export = client.get(
            f"/matters/{slug}/conversations/{conversation.conversation_id}/export",
            params={"format": "docx"},
        )
        assert conversation_export.status_code == 200
        with zipfile.ZipFile(io.BytesIO(conversation_export.content)) as archive:
            assert archive.testzip() is None

        bundle = client.get(f"/matters/{slug}/export")
        assert bundle.status_code == 200
        with zipfile.ZipFile(io.BytesIO(bundle.content)) as archive:
            manifest = json.loads(archive.read("manifest.json"))
            assert manifest["original_source_files_included"] is False
            assert manifest["source_count"] == 1

        other = bench.matter(other_slug, WEB_ACTOR)
        other_conversation = bench.workspace.get_conversation(other.matter_id)
        assert client.get(
            f"/matters/{other_slug}/conversations/{conversation.conversation_id}/messages/{answer.message_id}/export"
        ).status_code == 404
        assert client.get(
            f"/matters/{slug}/conversations/{other_conversation.conversation_id}/export"
        ).status_code == 404

        wrong = client.post(
            f"/matters/{slug}/close",
            data={"confirmed_name": "Wrong name", "acknowledge": "yes"},
            follow_redirects=False,
        )
        assert wrong.status_code == 303
        assert bench.workspace.matter_lifecycle(matter.matter_id).state == "active"

        close_page = client.get(f"/matters/{slug}/close")
        assert close_page.status_code == 200
        assert "Begin closure" in close_page.text
        assert "separate, fresh confirmation" in close_page.text
        assert "External registered originals are not deleted" in close_page.text

        confirmation = prepare_preview_exit(client, matter)
        deleted = client.post(
            f"/matters/{slug}/close",
            data=confirmation,
            follow_redirects=False,
        )
        assert deleted.status_code == 303
        assert deleted.headers["location"].startswith("/matters/new?notice=")
        assert client.get(f"/matters/{slug}").status_code == 404
        assert not (runtime / "matters" / matter.matter_id).exists()
        assert bench.workspace.matter_lifecycle(matter.matter_id).state == "deleted"
        assert all(item.slug != slug for item in bench.matters(WEB_ACTOR))
        assert any(
            event.action == "matter.purge_confirm" and event.outcome == "success"
            for event in bench.workspace.audit_events(matter.matter_id)
        )


def test_failed_storage_purge_is_hidden_from_review_and_owner_can_retry(tmp_path, monkeypatch):
    import case_intelligence.workbench as workbench_module

    runtime = tmp_path / "runtime"
    original_delete = workbench_module.securely_delete_owned_tree
    calls = 0

    def fail_once(path, *, allowed_parent):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("generated deletion failure")
        return original_delete(path, allowed_parent=allowed_parent)

    monkeypatch.setattr(workbench_module, "securely_delete_owned_tree", fail_once)
    with TestClient(
        create_workbench_app(
            runtime,
            generator=UnavailableGenerator(),
            auth_mode="preview",
            answer_workers=1,
        )
    ) as client:
        slug = _create_matter(client, "Synthetic retryable purge")
        matter = client.app.state.workbench.matter(slug, WEB_ACTOR)
        confirmation = prepare_preview_exit(client, matter)
        first = client.post(
            f"/matters/{slug}/close",
            data=confirmation,
            follow_redirects=False,
        )
        assert first.status_code == 303
        lifecycle = client.app.state.workbench.workspace.matter_lifecycle(
            matter.matter_id
        )
        assert lifecycle.state == "purge_failed"
        assert client.get(f"/matters/{slug}").status_code == 404
        recovery = client.get(f"/matters/{slug}/close")
        assert recovery.status_code == 200
        assert "confirmed deletion did not finish" in recovery.text
        assert "Review final deletion confirmation" in recovery.text

        confirmation = prepare_preview_exit(client, matter)
        retried = client.post(
            f"/matters/{slug}/close",
            data=confirmation,
            follow_redirects=False,
        )
        assert retried.status_code == 303
        assert client.app.state.workbench.workspace.matter_lifecycle(
            matter.matter_id
        ).state == "deleted"
        assert not any((runtime / ".matter-purging").iterdir())


def test_clip_export_lease_blocks_purge_and_strict_cleanup_failure_is_retryable(
    tmp_path, monkeypatch
):
    import case_intelligence.workbench as workbench_module

    runtime = tmp_path / "runtime"
    with TestClient(
        create_workbench_app(
            runtime,
            generator=UnavailableGenerator(),
            auth_mode="preview",
            answer_workers=1,
        )
    ) as client:
        slug = _create_matter(client, "Synthetic clip export purge boundary")
        bench = client.app.state.workbench
        matter = bench.matter(slug, WEB_ACTOR)
        lease = bench.begin_media_clip_export(matter)
        rendered = lease / "generated-clip.wav"
        rendered.write_bytes(b"RIFF generated clip work product")
        assert bench.matter_active_work_counts(matter.matter_id)["exports"] == 1

        close_page = client.get(f"/matters/{slug}/close")
        assert close_page.status_code == 200
        assert "Wait for active processing and downloads" in close_page.text
        refused = client.post(
            f"/matters/{slug}/close/begin",
            data={"confirmed_name": matter.display_name, "acknowledge": "yes"},
            follow_redirects=False,
        )
        assert refused.status_code == 303
        assert bench.workspace.matter_lifecycle(matter.matter_id).state == "active"
        assert rendered.is_file()

        clip_root = runtime / "matters" / matter.matter_id / "clip-exports"
        original_delete = workbench_module.securely_delete_owned_tree
        failed_targets = set()

        def fail_each_clip_cleanup_once(path, *, allowed_parent):
            if path in {lease, clip_root} and path not in failed_targets:
                failed_targets.add(path)
                raise RuntimeError("synthetic strict clip cleanup failure")
            return original_delete(path, allowed_parent=allowed_parent)

        monkeypatch.setattr(
            workbench_module,
            "securely_delete_owned_tree",
            fail_each_clip_cleanup_once,
        )
        assert bench.finish_media_clip_export(matter.matter_id, lease) is False
        assert bench.matter_active_work_counts(matter.matter_id)["exports"] == 0
        assert lease.is_dir()
        confirmation = prepare_preview_exit(client, matter)
        failed = client.post(
            f"/matters/{slug}/close",
            data=confirmation,
            follow_redirects=False,
        )
        assert failed.status_code == 303
        lifecycle = bench.workspace.matter_lifecycle(matter.matter_id)
        assert lifecycle.state == "purge_failed"
        assert lifecycle.error_code == "clip_exports"
        assert lease.is_dir()

        recovery = client.get(f"/matters/{slug}/close")
        assert recovery.status_code == 200
        assert "confirmed deletion did not finish" in recovery.text
        confirmation = prepare_preview_exit(client, matter)
        retried = client.post(
            f"/matters/{slug}/close",
            data=confirmation,
            follow_redirects=False,
        )
        assert retried.status_code == 303
        assert bench.workspace.matter_lifecycle(matter.matter_id).state == "deleted"
        assert not (runtime / "matters" / matter.matter_id).exists()
