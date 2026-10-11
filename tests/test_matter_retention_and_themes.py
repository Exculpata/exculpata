from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from case_intelligence.generation import UnavailableGenerator
from case_intelligence.workbench import CaseIntelligenceWorkbench, create_workbench_app
from case_intelligence.workspace_store import WorkspaceProblem, WorkspaceStore


FIXED = datetime(2026, 8, 28, 16, 0, tzinfo=timezone.utc)


def _principals(store: WorkspaceStore) -> None:
    store.upsert_principal(
        "test", "owner", "Owner User", "owner", preferred_principal_id="principal-owner"
    )
    store.upsert_principal(
        "test", "member", "Member User", "member", preferred_principal_id="principal-member"
    )
    store.upsert_principal(
        "test", "admin", "Admin User", "admin", preferred_principal_id="principal-admin"
    )


def test_retention_migration_is_mirrored_and_existing_matters_are_grandfathered(tmp_path):
    root = Path(__file__).resolve().parents[1]
    assert (
        root / "migrations/sqlite/0017_matter_retention_and_preferences.sql"
    ).read_bytes() == (
        root
        / "src/case_intelligence/migrations/sqlite/0017_matter_retention_and_preferences.sql"
    ).read_bytes()

    store = WorkspaceStore(tmp_path / "workbench.sqlite", clock=lambda: FIXED)
    _principals(store)
    grandfathered = store.create_matter(
        "Existing synthetic matter", "", "principal-owner"
    )
    scheduled = store.create_matter(
        "New synthetic matter", "", "principal-owner", retention_days=30
    )

    assert store.matter_retention(grandfathered.matter_id) is None
    retention = store.matter_retention(scheduled.matter_id)
    assert retention is not None
    assert retention.expires_at == "2026-09-27T16:00:00Z"
    assert retention.purge_after == "2026-10-04T16:00:00Z"
    assert store.due_matter_retentions() == ()
    store.close()


def test_owner_or_administrator_can_reschedule_but_member_cannot(tmp_path):
    store = WorkspaceStore(tmp_path / "workbench.sqlite", clock=lambda: FIXED)
    _principals(store)
    matter = store.create_matter("Synthetic retention roles", "", "principal-owner")
    store.add_member(matter.matter_id, "principal-member", "principal-owner")

    with pytest.raises(WorkspaceProblem, match="Only the matter owner"):
        store.set_matter_retention(
            matter.matter_id,
            "principal-member",
            FIXED + timedelta(days=20),
        )

    owner_schedule = store.set_matter_retention(
        matter.matter_id,
        "principal-owner",
        FIXED + timedelta(days=20),
    )
    assert owner_schedule.scheduled_by == "principal-owner"
    admin_schedule = store.set_matter_retention(
        matter.matter_id,
        "principal-admin",
        FIXED + timedelta(days=40),
        administrator_override=True,
    )
    assert admin_schedule.scheduled_by == "principal-admin"
    assert admin_schedule.purge_after == "2026-10-14T16:00:00Z"
    store.close()


def test_due_claim_rechecks_schedule_and_waits_for_active_upload(tmp_path):
    now = [FIXED]
    store = WorkspaceStore(tmp_path / "workbench.sqlite", clock=lambda: now[0])
    _principals(store)
    matter = store.create_matter(
        "Synthetic due matter", "", "principal-owner", retention_days=1
    )
    session, _ = store.create_upload_session(
        matter.matter_id,
        "principal-owner",
        "Generated upload",
        (
            {
                "display_name": "generated.txt",
                "relative_path": "generated.txt",
                "media_type": "text/plain",
                "expected_size": 12,
            },
        ),
    )
    now[0] = FIXED + timedelta(days=9)
    assert len(store.due_matter_retentions()) == 1
    assert store.begin_due_matter_purge(matter.matter_id, source_count=0) is None

    store.cancel_upload_session(
        matter.matter_id, "principal-owner", session.upload_session_id
    )
    claimed = store.begin_due_matter_purge(matter.matter_id, source_count=0)
    assert claimed is not None
    assert claimed[1].state == "purging"
    assert claimed[1].requested_by == "principal-owner"
    store.close()


def test_abandoned_upload_detection_and_theme_preference(tmp_path):
    now = [FIXED]
    store = WorkspaceStore(tmp_path / "workbench.sqlite", clock=lambda: now[0])
    _principals(store)
    matter = store.create_matter("Synthetic cleanup matter", "", "principal-owner")
    session, _ = store.create_upload_session(
        matter.matter_id,
        "principal-owner",
        "Generated upload",
        (
            {
                "display_name": "generated.txt",
                "relative_path": "generated.txt",
                "media_type": "text/plain",
                "expected_size": 32,
            },
        ),
    )
    assert store.abandoned_upload_sessions() == ()
    now[0] = FIXED + timedelta(hours=25)
    assert store.abandoned_upload_sessions() == (session,)

    assert store.principal_preference("principal-owner").theme == "dark"
    assert store.set_principal_theme("principal-owner", "dusk").theme == "dusk"
    assert store.set_principal_theme("principal-owner", "dark").theme == "dark"
    assert store.principal_preference("principal-owner").theme == "dark"
    with pytest.raises(WorkspaceProblem, match="available appearance"):
        store.set_principal_theme("principal-owner", "unreviewed-theme")
    store.close()


def test_supported_maintenance_cleans_staging_and_purges_only_scheduled_due_matter(
    tmp_path,
):
    bench = CaseIntelligenceWorkbench(
        tmp_path / "runtime",
        generator=UnavailableGenerator(),
        learned_retrieval=False,
    )
    try:
        bench.workspace._clock = lambda: FIXED
        bench.workspace.upsert_principal(
            "test",
            "owner",
            "Owner User",
            "owner",
            preferred_principal_id="principal-owner",
        )
        grandfathered = bench.create_matter(
            "Grandfathered synthetic matter", "", "principal-owner"
        )
        due = bench.create_matter(
            "Scheduled synthetic matter",
            "",
            "principal-owner",
            retention_days=1,
        )
        session, items = bench.create_upload_session(
            grandfathered,
            "principal-owner",
            "Generated abandoned upload",
            (
                {
                    "display_name": "generated.txt",
                    "relative_path": "generated.txt",
                    "media_type": "text/plain",
                    "expected_size": 9,
                },
            ),
        )
        store = bench.source_store(grandfathered)
        store.append_resumable_chunk(
            items[0].upload_item_id,
            offset=0,
            expected_size=9,
            chunk=b"generated",
        )
        bench.workspace.set_upload_item_offset(
            grandfathered.matter_id,
            "principal-owner",
            session.upload_session_id,
            items[0].upload_item_id,
            0,
            9,
        )
        bench.workspace._clock = lambda: FIXED + timedelta(days=9)

        result = bench.run_maintenance_once()
        assert result == {
            "uploads_cleaned": 1,
            "matters_purged": 1,
            "matters_deferred": 0,
        }
        incoming = store.incoming / store._resumable_name(items[0].upload_item_id)
        assert not incoming.exists()
        assert bench.workspace.get_matter_by_id(grandfathered.matter_id) == grandfathered
        with pytest.raises(KeyError):
            bench.workspace.get_matter_by_id(due.matter_id)
        assert bench.workspace.matter_lifecycle(due.matter_id).state == "deleted"
    finally:
        bench.close()


def test_temporary_matter_theme_lifecycle_and_persistent_assistant_routes(tmp_path):
    app = create_workbench_app(
        tmp_path / "runtime",
        generator=UnavailableGenerator(),
        auth_mode="test",
        answer_workers=1,
    )
    with TestClient(app) as client:
        new_page = client.get("/matters/new")
        assert new_page.status_code == 200
        assert 'name="retention_days"' in new_page.text
        assert "seven-day export grace period" in new_page.text

        created = client.post(
            "/matters",
            data={
                "name": "Synthetic temporary review",
                "descriptor": "Generated interface fixture",
                "retention_days": "30",
            },
            follow_redirects=False,
        )
        assert created.status_code == 303
        slug = created.headers["location"].split("/")[2]
        matter = app.state.workbench.workspace.get_active_matter(slug)
        assert app.state.workbench.workspace.matter_retention(matter.matter_id) is not None

        setup = client.get(f"/matters/{slug}/setup")
        assert setup.status_code == 200
        assert "data-assistant-dock" in setup.text
        assert "Ask Exculpata" in setup.text
        assert 'data-theme="dark"' in setup.text
        assert 'value="dark"' in setup.text
        assert 'value="light"' in setup.text
        assert 'value="dusk"' in setup.text
        identifiers = re.findall(r'\bid="([^"]+)"', setup.text)
        assert len(identifiers) == len(set(identifiers))
        for accessible_control in (
            "data-rail-toggle",
            'aria-label="Open account menu"',
            'data-assistant-expand aria-label="Open Ask Exculpata"',
            'data-assistant-collapse aria-label="Close Ask Exculpata"',
            'data-assistant-new-chat',
            'aria-label="Start a new chat"',
            'for="assistant-conversation-picker"',
            'for="matter-filter"',
            'for="assistant-question"',
        ):
            assert accessible_control in setup.text

        assistant = client.get(f"/matters/{slug}/assistant")
        assert assistant.status_code == 200
        assert "data-assistant-question-form" in assistant.text
        assert "Choose a saved chat" in assistant.text
        assert "New chat" in assistant.text

        new_chat = client.post(
            f"/matters/{slug}/conversations",
            headers={"Accept": "application/json"},
        )
        assert new_chat.status_code == 201
        created_chat = new_chat.json()
        assert created_chat["title"] == "New conversation"
        assert created_chat["conversation_id"] in created_chat["fragment_url"]
        switched = client.get(created_chat["fragment_url"])
        assert switched.status_code == 200
        assert (
            f'data-conversation-id="{created_chat["conversation_id"]}"'
            in switched.text
        )
        picker = re.search(
            r'<select id="assistant-conversation-picker".*?</select>',
            switched.text,
            re.DOTALL,
        )
        assert picker is not None
        assert picker.group(0).count("<option value=") == 2

        full_review = client.get(f"/matters/{slug}")
        assert full_review.status_code == 200
        assert 'id="conversation-heading">Ask</h2>' in full_review.text
        assert "data-assistant-dock" not in full_review.text

        themed = client.post(
            "/preferences/theme",
            data={"theme": "dusk", "next": f"/matters/{slug}/setup"},
            follow_redirects=False,
        )
        assert themed.status_code == 303
        assert themed.headers["location"] == f"/matters/{slug}/setup"
        dusk = client.get(f"/matters/{slug}/setup")
        assert 'data-theme="dusk"' in dusk.text
        light = client.post(
            "/preferences/theme",
            data={"theme": "light", "next": f"/matters/{slug}/setup"},
            follow_redirects=False,
        )
        assert light.status_code == 303
        assert 'data-theme="light"' in client.get(f"/matters/{slug}/setup").text
        dark = client.post(
            "/preferences/theme",
            data={"theme": "dark", "next": f"/matters/{slug}/setup"},
            follow_redirects=False,
        )
        assert dark.status_code == 303
        assert 'data-theme="dark"' in client.get(f"/matters/{slug}/setup").text

        new_expiry = (datetime.now(timezone.utc) + timedelta(days=45)).date().isoformat()
        extended = client.post(
            f"/matters/{slug}/retention",
            data={"expires_on": new_expiry, "next": f"/matters/{slug}/setup"},
            follow_redirects=False,
        )
        assert extended.status_code == 303
        assert extended.headers["location"] == f"/matters/{slug}/setup"

        health = client.get("/health").json()
        assert health["maintenance"]["enabled"] is False
