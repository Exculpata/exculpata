from __future__ import annotations

import io
import json
import re
import threading
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from case_intelligence.generation import UnavailableGenerator
from case_intelligence.identity import LOGIN_CHALLENGE_COOKIE, SESSION_COOKIE
from case_intelligence.workbench import create_workbench_app
from case_intelligence.workspace_store import WorkspaceProblem, WorkspaceStore


FIXED = datetime(2026, 8, 27, 19, 0, tzinfo=timezone.utc)


def _seed_people(store: WorkspaceStore) -> tuple[str, str]:
    owner = store.upsert_principal(
        "test", "owner", "Owner User", "owner", preferred_principal_id="principal-owner"
    )
    member = store.upsert_principal(
        "test", "member", "Member User", "member", preferred_principal_id="principal-member"
    )
    return owner.principal_id, member.principal_id


def _seed_research_job(
    store: WorkspaceStore,
    matter_id: str,
    conversation_id: str,
    actor_id: str,
    *,
    ordinal: int,
    state: str,
):
    job, created = store.queue_research_job(
        matter_id,
        actor_id,
        f"What does generated investigation {ordinal} show?",
        f"Generated investigation {ordinal}",
        f"research-request-{ordinal:032x}",
        conversation_id=conversation_id,
    )
    assert created is True
    if state == "queued":
        return job
    if state == "cancelled":
        return store.cancel_research_job(matter_id, actor_id, job.job_id)
    claimed = store.claim_research_job(f"generated-research-worker-{ordinal}")
    assert claimed is not None and claimed.job_id == job.job_id
    if state == "running":
        return claimed
    if state == "failed":
        return store.fail_research_job(
            job.job_id, f"Generated failed investigation {ordinal}."
        )
    assert state == "succeeded"
    return store.finish_research_job(
        job.job_id,
        {
            "summary": "No supported finding was retained.",
            "passes": [
                {
                    "query": f"generated investigation {ordinal}",
                    "status": "gap",
                    "text": "No searchable passage matched this part of the research plan.",
                }
            ],
            "evidence": [],
            "coverage": {
                "search_pass_count": 1,
                "candidate_passage_count": 0,
                "evidence_passage_count": 0,
                "evidence_source_count": 0,
                "scope": "matter",
                "notice": "Synthetic deletion fixture.",
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


def test_conversation_organization_persists_orders_and_keeps_archived_exportable(tmp_path):
    path = tmp_path / "workbench.sqlite"
    store = WorkspaceStore(path, clock=lambda: FIXED)
    owner, member = _seed_people(store)
    matter = store.create_matter("Synthetic organization matter", "", owner)
    store.add_member(matter.matter_id, member, owner)
    first = store.get_conversation(matter.matter_id)
    second = store.create_conversation(matter.matter_id, "Timeline", actor_id=member)
    store.append_message(matter.matter_id, first.conversation_id, "user", "Generated question")

    store.set_conversation_pinned(matter.matter_id, first.conversation_id, member, True)
    summaries = store.conversation_summaries(matter.matter_id)
    assert summaries[0].conversation_id == first.conversation_id
    assert summaries[0].is_pinned == 1
    assert summaries[0].state == "active"

    next_conversation = store.archive_conversation(
        matter.matter_id, first.conversation_id, member
    )
    assert next_conversation.conversation_id == second.conversation_id
    with pytest.raises(KeyError):
        store.get_conversation(matter.matter_id, first.conversation_id)
    assert store.get_conversation_any(matter.matter_id, first.conversation_id) == first
    assert store.messages(matter.matter_id, first.conversation_id)[0].content == "Generated question"
    archived = next(
        item for item in store.conversation_summaries(matter.matter_id)
        if item.conversation_id == first.conversation_id
    )
    assert archived.state == "archived"
    assert archived.is_pinned == 0
    assert archived.archived_at == "2026-08-27T19:00:00Z"
    store.close()

    reopened = WorkspaceStore(path, clock=lambda: FIXED)
    reopened.restore_conversation(matter.matter_id, first.conversation_id, owner)
    assert reopened.get_conversation(matter.matter_id, first.conversation_id) == first
    assert reopened.conversation_organization(first.conversation_id).state == "active"
    reopened.close()


def test_archive_and_delete_are_atomic_block_active_work_and_replace_last_thread(tmp_path):
    store = WorkspaceStore(tmp_path / "workbench.sqlite", clock=lambda: FIXED)
    owner, member = _seed_people(store)
    matter = store.create_matter("Synthetic deletion matter", "", owner)
    store.add_member(matter.matter_id, member, owner)
    first = store.get_conversation(matter.matter_id)
    store.append_message(matter.matter_id, first.conversation_id, "user", "Generated evidence question")
    job, _ = store.queue_answer_job(
        matter.matter_id,
        first.conversation_id,
        owner,
        "What does the generated record show?",
        "answer-request-" + "a" * 32,
    )

    with pytest.raises(WorkspaceProblem, match="answer in progress"):
        store.archive_conversation(matter.matter_id, first.conversation_id, member)
    with pytest.raises(WorkspaceProblem, match="answer in progress"):
        store.delete_conversation(
            matter.matter_id,
            first.conversation_id,
            owner,
            confirmed_title=first.title,
            acknowledged=True,
        )
    store.cancel_answer_job(matter.matter_id, owner, job.job_id)
    replacement = store.archive_conversation(
        matter.matter_id, first.conversation_id, member
    )
    assert replacement.conversation_id != first.conversation_id
    assert replacement.title == "New conversation"
    store.restore_conversation(matter.matter_id, first.conversation_id, owner)

    with pytest.raises(WorkspaceProblem, match="Only a matter owner"):
        store.delete_conversation(
            matter.matter_id,
            first.conversation_id,
            member,
            confirmed_title=first.title,
            acknowledged=True,
        )
    with pytest.raises(WorkspaceProblem, match="exactly"):
        store.delete_conversation(
            matter.matter_id,
            first.conversation_id,
            owner,
            confirmed_title="Wrong title",
            acknowledged=True,
        )
    with pytest.raises(WorkspaceProblem, match="cannot be undone"):
        store.delete_conversation(
            matter.matter_id,
            first.conversation_id,
            owner,
            confirmed_title=first.title,
            acknowledged=False,
        )

    deleted = store.delete_conversation(
        matter.matter_id,
        first.conversation_id,
        owner,
        confirmed_title=first.title,
        acknowledged=True,
    )
    assert deleted.message_count == 2
    assert deleted.answer_job_count == 1
    assert deleted.next_conversation_id == replacement.conversation_id
    assert store.connection.execute(
        "SELECT COUNT(*) FROM workbench_answer_event WHERE job_id=?", (job.job_id,)
    ).fetchone()[0] == 0
    assert store.connection.execute(
        "SELECT COUNT(*) FROM workbench_conversation_organization WHERE conversation_id=?",
        (first.conversation_id,),
    ).fetchone()[0] == 0
    assert store.messages(matter.matter_id, replacement.conversation_id) == ()
    with pytest.raises(KeyError):
        store.get_conversation_any(matter.matter_id, first.conversation_id)
    store.close()


def test_conversation_deletion_accounts_for_and_removes_linked_investigations(tmp_path):
    store = WorkspaceStore(tmp_path / "workbench.sqlite", clock=lambda: FIXED)
    owner, _member = _seed_people(store)
    matter = store.create_matter("Synthetic investigation deletion", "", owner)
    conversation = store.create_conversation(
        matter.matter_id, "Investigation deletion record", actor_id=owner
    )
    succeeded = _seed_research_job(
        store,
        matter.matter_id,
        conversation.conversation_id,
        owner,
        ordinal=1,
        state="succeeded",
    )
    failed = _seed_research_job(
        store,
        matter.matter_id,
        conversation.conversation_id,
        owner,
        ordinal=2,
        state="failed",
    )
    active = _seed_research_job(
        store,
        matter.matter_id,
        conversation.conversation_id,
        owner,
        ordinal=3,
        state="queued",
    )
    other_matter = store.create_matter("Synthetic other investigation matter", "", owner)
    other_conversation = store.create_conversation(
        other_matter.matter_id, "Other investigation record", actor_id=owner
    )
    other_research = _seed_research_job(
        store,
        other_matter.matter_id,
        other_conversation.conversation_id,
        owner,
        ordinal=8,
        state="failed",
    )

    counts = store.conversation_content_counts(
        matter.matter_id, conversation.conversation_id
    )
    assert counts["research_jobs"] == 3
    assert counts["succeeded_research"] == 1
    assert counts["active_research"] == 1
    with pytest.raises(KeyError):
        store.conversation_content_counts(
            other_matter.matter_id, conversation.conversation_id
        )
    with pytest.raises(WorkspaceProblem, match="investigation in progress"):
        store.delete_conversation(
            matter.matter_id,
            conversation.conversation_id,
            owner,
            confirmed_title=conversation.title,
            acknowledged=True,
        )

    store.cancel_research_job(matter.matter_id, owner, active.job_id)
    deleted = store.delete_conversation(
        matter.matter_id,
        conversation.conversation_id,
        owner,
        confirmed_title=conversation.title,
        acknowledged=True,
    )

    assert deleted.research_job_count == 3
    assert store.connection.execute(
        "SELECT COUNT(*) FROM workbench_research_job WHERE job_id IN (?,?,?)",
        (succeeded.job_id, failed.job_id, active.job_id),
    ).fetchone()[0] == 0
    assert store.connection.execute(
        "SELECT COUNT(*) FROM workbench_research_event WHERE job_id IN (?,?,?)",
        (succeeded.job_id, failed.job_id, active.job_id),
    ).fetchone()[0] == 0
    assert store.connection.execute(
        "SELECT COUNT(*) FROM workbench_research_job WHERE job_id=?",
        (other_research.job_id,),
    ).fetchone()[0] == 1
    store.close()


def test_conversation_archive_serializes_against_new_investigation_admission(
    tmp_path, monkeypatch
):
    path = tmp_path / "workbench.sqlite"
    archiving = WorkspaceStore(path, clock=lambda: FIXED)
    owner, _member = _seed_people(archiving)
    matter = archiving.create_matter("Synthetic archive race", "", owner)
    conversation = archiving.create_conversation(
        matter.matter_id, "Archive race record", actor_id=owner
    )
    contender = WorkspaceStore(path, clock=lambda: FIXED)
    attempted = threading.Event()
    finished = threading.Event()
    outcome: list[object] = []

    def admit_investigation() -> None:
        attempted.set()
        try:
            contender.queue_research_job(
                matter.matter_id,
                owner,
                "Can a generated investigation enter during archive?",
                "Generated archive race",
                "research-request-" + "b" * 32,
                conversation_id=conversation.conversation_id,
            )
        except Exception as exc:  # The exact staff-safe rejection is asserted below.
            outcome.append(exc)
        finally:
            finished.set()

    thread = threading.Thread(target=admit_investigation, daemon=True)
    original_active_answer_check = archiving._conversation_has_active_answer_locked

    def observe_write_lock(conversation_id: str) -> bool:
        thread.start()
        assert attempted.wait(timeout=1)
        assert not finished.wait(timeout=0.25), (
            "investigation admission escaped the conversation archive transaction"
        )
        return original_active_answer_check(conversation_id)

    monkeypatch.setattr(
        archiving, "_conversation_has_active_answer_locked", observe_write_lock
    )
    replacement = archiving.archive_conversation(
        matter.matter_id, conversation.conversation_id, owner
    )
    thread.join(timeout=5)

    assert replacement.conversation_id != conversation.conversation_id
    assert archiving.conversation_summary(
        matter.matter_id, conversation.conversation_id
    ).state == "archived"
    assert finished.is_set()
    assert len(outcome) == 1
    assert isinstance(outcome[0], WorkspaceProblem)
    assert "not available in this matter" in str(outcome[0])
    assert contender.connection.execute(
        "SELECT COUNT(*) FROM workbench_research_job WHERE conversation_id=?",
        (conversation.conversation_id,),
    ).fetchone()[0] == 0
    assert contender.messages(matter.matter_id, conversation.conversation_id) == ()
    contender.close()
    archiving.close()


def test_conversation_deletion_serializes_against_new_investigation_admission(
    tmp_path, monkeypatch
):
    path = tmp_path / "workbench.sqlite"
    deleting = WorkspaceStore(path, clock=lambda: FIXED)
    owner, _member = _seed_people(deleting)
    matter = deleting.create_matter("Synthetic deletion race", "", owner)
    conversation = deleting.create_conversation(
        matter.matter_id, "Deletion race record", actor_id=owner
    )
    contender = WorkspaceStore(path, clock=lambda: FIXED)
    attempted = threading.Event()
    finished = threading.Event()
    outcome: list[object] = []

    def admit_investigation() -> None:
        attempted.set()
        try:
            contender.queue_research_job(
                matter.matter_id,
                owner,
                "Can a generated investigation enter during deletion?",
                "Generated deletion race",
                "research-request-" + "a" * 32,
                conversation_id=conversation.conversation_id,
            )
        except Exception as exc:  # The exact staff-safe rejection is asserted below.
            outcome.append(exc)
        finally:
            finished.set()

    thread = threading.Thread(target=admit_investigation, daemon=True)
    original_active_answer_check = deleting._conversation_has_active_answer_locked

    def observe_write_lock(conversation_id: str) -> bool:
        thread.start()
        assert attempted.wait(timeout=1)
        assert not finished.wait(timeout=0.25), (
            "investigation admission escaped the conversation deletion transaction"
        )
        return original_active_answer_check(conversation_id)

    monkeypatch.setattr(
        deleting, "_conversation_has_active_answer_locked", observe_write_lock
    )
    deleted = deleting.delete_conversation(
        matter.matter_id,
        conversation.conversation_id,
        owner,
        confirmed_title=conversation.title,
        acknowledged=True,
    )
    thread.join(timeout=5)

    assert deleted.conversation_id == conversation.conversation_id
    assert finished.is_set()
    assert len(outcome) == 1
    assert isinstance(outcome[0], WorkspaceProblem)
    assert "not available in this matter" in str(outcome[0])
    assert contender.connection.execute(
        "SELECT COUNT(*) FROM workbench_research_job WHERE conversation_id=?",
        (conversation.conversation_id,),
    ).fetchone()[0] == 0
    contender.close()
    deleting.close()


def test_conversation_deletion_rolls_back_linked_investigation_cleanup(
    tmp_path, monkeypatch
):
    store = WorkspaceStore(tmp_path / "workbench.sqlite", clock=lambda: FIXED)
    owner, _member = _seed_people(store)
    matter = store.create_matter("Synthetic deletion rollback", "", owner)
    conversation = store.create_conversation(
        matter.matter_id, "Deletion rollback record", actor_id=owner
    )
    research = _seed_research_job(
        store,
        matter.matter_id,
        conversation.conversation_id,
        owner,
        ordinal=4,
        state="succeeded",
    )

    def fail_replacement(*_args, **_kwargs):
        raise RuntimeError("synthetic replacement failure")

    monkeypatch.setattr(store, "_ensure_active_conversation_locked", fail_replacement)
    with pytest.raises(RuntimeError, match="synthetic replacement failure"):
        store.delete_conversation(
            matter.matter_id,
            conversation.conversation_id,
            owner,
            confirmed_title=conversation.title,
            acknowledged=True,
        )

    assert store.get_conversation_any(
        matter.matter_id, conversation.conversation_id
    ) == conversation
    assert store.connection.execute(
        "SELECT json_extract(result_json,'$.summary') "
        "FROM workbench_research_job WHERE job_id=?",
        (research.job_id,),
    ).fetchone()[0] == "No supported finding was retained."
    assert store.connection.execute(
        "SELECT COUNT(*) FROM workbench_research_event WHERE job_id=?",
        (research.job_id,),
    ).fetchone()[0] == 3
    store.close()


def _login(client: TestClient, subject: str, next_path: str = "/") -> tuple[str, str]:
    page = client.get("/auth/login", params={"next": next_path})
    challenge = client.cookies.get(LOGIN_CHALLENGE_COOKIE)
    assert page.status_code == 200 and challenge
    response = client.post(
        "/auth/login",
        data={
            "identity_subject": subject,
            "login_challenge": challenge,
            "next": next_path,
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    token = client.cookies.get(SESSION_COOKIE)
    landing = client.get(response.headers["location"])
    csrf = re.search(r'data-csrf-token="([0-9a-f]{64})"', landing.text)
    assert token and csrf
    return token, csrf.group(1)


def _set_session(client: TestClient, token: str) -> str:
    client.cookies.set(SESSION_COOKIE, token, path="/")
    page = client.get("/")
    csrf = re.search(r'data-csrf-token="([0-9a-f]{64})"', page.text)
    assert csrf
    return csrf.group(1)


def test_routes_group_filter_archive_restore_and_restrict_permanent_delete(tmp_path):
    with TestClient(
        create_workbench_app(
            tmp_path / "runtime",
            generator=UnavailableGenerator(),
            auth_mode="preview",
            answer_workers=1,
        )
    ) as client:
        owner_token, owner_csrf = _login(client, "taylor-morgan", "/matters/new")
        created = client.post(
            "/matters",
            data={
                "name": "Synthetic conversation routes",
                "descriptor": "Generated route fixture",
                "csrf_token": owner_csrf,
            },
            follow_redirects=False,
        )
        slug = re.fullmatch(
            r"/matters/(m-[0-9a-f]{12})/setup", created.headers["location"]
        ).group(1)
        bench = client.app.state.workbench
        matter = bench.matter(slug, "development-taylor-morgan")
        first = bench.workspace.get_conversation(matter.matter_id)
        second = bench.workspace.create_conversation(
            matter.matter_id, "Generated timeline", actor_id="development-taylor-morgan"
        )
        client.post(
            f"/matters/{slug}/members",
            data={
                "principal_id": "development-jordan-lee",
                "csrf_token": owner_csrf,
            },
        )

        client.cookies.delete(SESSION_COOKIE, path="/")
        member_token, member_csrf = _login(client, "jordan-lee")
        pinned = client.post(
            f"/matters/{slug}/conversations/{first.conversation_id}/pin",
            data={"csrf_token": member_csrf},
            follow_redirects=False,
        )
        assert pinned.status_code == 303
        archived = client.post(
            f"/matters/{slug}/conversations/{first.conversation_id}/archive",
            data={"csrf_token": member_csrf},
            follow_redirects=False,
        )
        assert archived.status_code == 303
        page = client.get(f"/matters/{slug}")
        assert 'data-conversation-filter' in page.text
        assert "Archived" in page.text
        assert first.title in page.text
        assert "Generated timeline" in page.text
        assert client.get(
            f"/matters/{slug}/conversations/{first.conversation_id}/export"
        ).status_code == 200
        assert client.get(
            f"/matters/{slug}/conversations/{first.conversation_id}/delete"
        ).status_code == 403
        assert client.post(
            f"/matters/{slug}/conversations/{first.conversation_id}/delete",
            data={
                "confirmed_title": first.title,
                "acknowledge": "yes",
                "csrf_token": member_csrf,
            },
        ).status_code == 403

        restored = client.post(
            f"/matters/{slug}/conversations/{first.conversation_id}/restore",
            data={"csrf_token": member_csrf},
            follow_redirects=False,
        )
        assert restored.status_code == 303

        client.cookies.delete(SESSION_COOKIE, path="/")
        owner_csrf = _set_session(client, owner_token)
        delete_page = client.get(
            f"/matters/{slug}/conversations/{first.conversation_id}/delete"
        )
        assert delete_page.status_code == 200
        assert "Save as Word" in delete_page.text
        assert "cannot be undone" in delete_page.text
        wrong = client.post(
            f"/matters/{slug}/conversations/{first.conversation_id}/delete",
            data={
                "confirmed_title": "Wrong title",
                "acknowledge": "yes",
                "csrf_token": owner_csrf,
            },
            follow_redirects=False,
        )
        assert wrong.status_code == 303
        assert bench.workspace.get_conversation_any(
            matter.matter_id, first.conversation_id
        ).conversation_id == first.conversation_id
        deleted = client.post(
            f"/matters/{slug}/conversations/{first.conversation_id}/delete",
            data={
                "confirmed_title": first.title,
                "acknowledge": "yes",
                "csrf_token": owner_csrf,
            },
            follow_redirects=False,
        )
        assert deleted.status_code == 303
        assert second.conversation_id in deleted.headers["location"]
        with pytest.raises(KeyError):
            bench.workspace.get_conversation_any(matter.matter_id, first.conversation_id)

        other_created = client.post(
            "/matters",
            data={
                "name": "Synthetic other route matter",
                "descriptor": "Generated isolation fixture",
                "csrf_token": owner_csrf,
            },
            follow_redirects=False,
        )
        other_slug = re.fullmatch(
            r"/matters/(m-[0-9a-f]{12})/setup", other_created.headers["location"]
        ).group(1)
        assert client.post(
            f"/matters/{other_slug}/conversations/{second.conversation_id}/archive",
            data={"csrf_token": owner_csrf},
        ).status_code == 404

        events = bench.workspace.audit_events(matter.matter_id)
        actions = {event.action for event in events}
        assert {"conversation.pin", "conversation.archive", "conversation.restore", "conversation.delete"} <= actions
        serialized = json.dumps([event.details for event in events])
        assert first.title not in serialized
        assert "Generated timeline" not in serialized


def test_delete_page_discloses_investigations_and_offers_a_complete_export(tmp_path):
    with TestClient(
        create_workbench_app(
            tmp_path / "runtime",
            generator=UnavailableGenerator(),
            auth_mode="preview",
            answer_workers=1,
        )
    ) as client:
        _owner_token, owner_csrf = _login(client, "taylor-morgan", "/matters/new")
        created = client.post(
            "/matters",
            data={
                "name": "Synthetic investigation disclosure",
                "descriptor": "Generated deletion confirmation fixture",
                "csrf_token": owner_csrf,
            },
            follow_redirects=False,
        )
        slug = re.fullmatch(
            r"/matters/(m-[0-9a-f]{12})/setup", created.headers["location"]
        ).group(1)
        bench = client.app.state.workbench
        # This fixture sets durable job states itself; the live worker must not
        # consume its queued example between disclosure and cancellation checks.
        assert bench.research is not None
        bench.research.close()
        matter = bench.matter(slug, "development-taylor-morgan")
        conversation = bench.workspace.get_conversation(matter.matter_id)
        conversation = bench.workspace.rename_conversation(
            matter.matter_id,
            conversation.conversation_id,
            "Generated investigation deletion",
        )
        bench.source_store(matter).store_stream(
            "generated-deletion-source.txt",
            "text/plain",
            io.BytesIO(b"Generated source for the deletion export fixture."),
        )
        bench._ensure_source_organizations(matter, bench.source_store(matter))
        succeeded = _seed_research_job(
            bench.workspace,
            matter.matter_id,
            conversation.conversation_id,
            "development-taylor-morgan",
            ordinal=5,
            state="succeeded",
        )
        _failed = _seed_research_job(
            bench.workspace,
            matter.matter_id,
            conversation.conversation_id,
            "development-taylor-morgan",
            ordinal=6,
            state="failed",
        )

        bundle = client.get(f"/matters/{slug}/export")
        assert bundle.status_code == 200, bundle.text
        with zipfile.ZipFile(io.BytesIO(bundle.content)) as archive:
            names = archive.namelist()
        assert any(
            name.startswith("investigations/") and name.endswith(".md")
            for name in names
        )
        active = _seed_research_job(
            bench.workspace,
            matter.matter_id,
            conversation.conversation_id,
            "development-taylor-morgan",
            ordinal=7,
            state="queued",
        )
        delete_url = (
            f"/matters/{slug}/conversations/{conversation.conversation_id}/delete"
        )
        page = client.get(delete_url)

        assert page.status_code == 200
        assert "<strong>3</strong><span>linked investigations</span>" in page.text
        assert "An investigation is still in progress" in page.text
        assert "progress history, evidence ledger, and saved result" in page.text
        assert (
            "Conversation-only exports do not include the full investigation records"
            in page.text
        )
        assert "Failed and cancelled investigation history is preserved only" in page.text
        assert f'href="/matters/{slug}/export"' in page.text
        assert "Download matter bundle" in page.text
        assert re.search(r"<button[^>]+disabled[^>]*>Delete conversation</button>", page.text)

        bench.workspace.cancel_research_job(
            matter.matter_id, "development-taylor-morgan", active.job_id
        )
        ready = client.get(delete_url)
        assert "An investigation is still in progress" not in ready.text
        assert not re.search(
            r"<button[^>]+disabled[^>]*>Delete conversation</button>", ready.text
        )
        deleted = client.post(
            delete_url,
            data={
                "confirmed_title": conversation.title,
                "acknowledge": "yes",
                "csrf_token": owner_csrf,
            },
            follow_redirects=False,
        )
        assert deleted.status_code == 303
        assert bench.workspace.connection.execute(
            "SELECT COUNT(*) FROM workbench_research_job WHERE conversation_id=?",
            (conversation.conversation_id,),
        ).fetchone()[0] == 0
        assert succeeded.job_id not in deleted.headers["location"]


def test_conversation_organization_migration_matches_operator_copy():
    root = Path(__file__).parents[1]
    assert (
        root / "src/case_intelligence/migrations/sqlite/0008_conversation_organization.sql"
    ).read_bytes() == (
        root / "migrations/sqlite/0008_conversation_organization.sql"
    ).read_bytes()
