from __future__ import annotations

from tests.fixture_write import fixture_write

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from case_intelligence.answer_jobs import AnswerJobFailure
from case_intelligence.generation import UnavailableGenerator
from case_intelligence.identity import LOGIN_CHALLENGE_COOKIE, SESSION_COOKIE, IdentityService
from case_intelligence.workbench import CaseIntelligenceWorkbench, create_workbench_app
from case_intelligence.workspace_store import WorkspaceStore


def _login(client: TestClient, subject: str, next_path: str = "/") -> tuple[str, str]:
    page = client.get("/auth/login", params={"next": next_path})
    assert page.status_code == 200
    challenge = client.cookies.get(LOGIN_CHALLENGE_COOKIE)
    assert challenge
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
    assert token
    landing = client.get(response.headers["location"])
    assert landing.status_code == 200
    matched = re.search(r'data-csrf-token="([0-9a-f]{64})"', landing.text)
    assert matched
    return token, matched.group(1)


def _set_session(client: TestClient, token: str) -> str:
    client.cookies.set(SESSION_COOKIE, token, path="/")
    page = client.get("/")
    assert page.status_code == 200
    matched = re.search(r'data-csrf-token="([0-9a-f]{64})"', page.text)
    assert matched
    return matched.group(1)


def _create_matter(client: TestClient, csrf: str) -> str:
    response = client.post(
        "/matters",
        data={
            "name": "Synthetic identity matter",
            "descriptor": "Generated access-control fixture",
            "csrf_token": csrf,
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    matched = re.fullmatch(r"/matters/(m-[0-9a-f]{12})/setup", response.headers["location"])
    assert matched
    return matched.group(1)


def test_preview_login_session_csrf_membership_revocation_and_audit(tmp_path):
    app = create_workbench_app(
        tmp_path / "runtime",
        generator=UnavailableGenerator(),
        auth_mode="preview",
        answer_workers=1,
    )
    with TestClient(app) as client:
        unauthenticated = client.get("/", follow_redirects=False)
        assert unauthenticated.status_code == 303
        assert unauthenticated.headers["location"].startswith("/auth/login")

        spoofed = client.get(
            "/matters/new",
            headers={"Remote-User": "spoofed@example.test"},
            follow_redirects=False,
        )
        assert spoofed.status_code == 303
        assert spoofed.headers["location"].startswith("/auth/login")

        taylor_token, taylor_csrf = _login(client, "taylor-morgan", "/matters/new")
        raw_rows = client.app.state.workbench.workspace.connection.execute(
            "SELECT token_digest FROM workbench_session"
        ).fetchall()
        assert [row[0] for row in raw_rows] == [hashlib.sha256(taylor_token.encode()).hexdigest()]
        database_bytes = (tmp_path / "runtime/workbench.sqlite").read_bytes()
        assert taylor_token.encode() not in database_bytes

        missing_csrf = client.post(
            "/matters",
            data={"name": "Must not be created", "descriptor": ""},
            follow_redirects=False,
        )
        assert missing_csrf.status_code == 403

        slug = _create_matter(client, taylor_csrf)
        matter = client.app.state.workbench.matter(slug, "development-taylor-morgan")

        add_jordan = client.post(
            f"/matters/{slug}/members",
            data={"principal_id": "development-jordan-lee", "csrf_token": taylor_csrf},
            follow_redirects=False,
        )
        assert add_jordan.status_code == 303

        client.cookies.delete(SESSION_COOKIE, path="/")
        jordan_token, jordan_csrf = _login(client, "jordan-lee")
        assert client.get(f"/matters/{slug}").status_code == 200
        assert client.get(f"/matters/{slug}/close").status_code == 404
        jordan_cannot_add = client.post(
            f"/matters/{slug}/members",
            data={"principal_id": "development-alex-rivera", "csrf_token": jordan_csrf},
        )
        assert jordan_cannot_add.status_code == 403

        client.cookies.delete(SESSION_COOKIE, path="/")
        _set_session(client, taylor_token)
        remove_jordan = client.post(
            f"/matters/{slug}/members/development-jordan-lee/remove",
            data={"csrf_token": taylor_csrf},
            follow_redirects=False,
        )
        assert remove_jordan.status_code == 303

        client.cookies.delete(SESSION_COOKIE, path="/")
        client.cookies.set(SESSION_COOKIE, jordan_token, path="/")
        assert client.get(f"/matters/{slug}").status_code == 404

        events = client.app.state.workbench.workspace.audit_events(matter.matter_id)
        assert any(
            event.action == "matter.create"
            and event.actor_principal_id == "development-taylor-morgan"
            and event.outcome == "success"
            for event in events
        )
        assert any(
            event.action == "membership.add"
            and event.actor_principal_id == "development-taylor-morgan"
            and event.object_id == "development-jordan-lee"
            for event in events
        )
        assert any(
            event.action == "membership.add"
            and event.actor_principal_id == "development-jordan-lee"
            and event.outcome == "denied"
            for event in events
        )
        assert any(
            event.action == "membership.revoke"
            and event.actor_principal_id == "development-taylor-morgan"
            for event in events
        )
        serialized = json.dumps([event.details for event in events])
        assert "Synthetic identity matter" not in serialized
        assert taylor_token not in serialized
        assert jordan_token not in serialized

        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            with fixture_write(client.app.state.workbench.workspace):
                client.app.state.workbench.workspace.connection.execute(
                    "UPDATE workbench_audit_event SET outcome='failure'"
                )
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            with fixture_write(client.app.state.workbench.workspace):
                client.app.state.workbench.workspace.connection.execute(
                    "DELETE FROM workbench_audit_event"
                )


def test_session_survives_restart_then_logout_revokes_replay(tmp_path):
    runtime = tmp_path / "runtime"
    with TestClient(
        create_workbench_app(
            runtime,
            generator=UnavailableGenerator(),
            auth_mode="preview",
            answer_workers=1,
        )
    ) as first:
        token, _csrf = _login(first, "taylor-morgan", "/matters/new")

    with TestClient(
        create_workbench_app(
            runtime,
            generator=UnavailableGenerator(),
            auth_mode="preview",
            answer_workers=1,
        )
    ) as restarted:
        csrf = _set_session(restarted, token)
        logout = restarted.post(
            "/auth/logout",
            data={"csrf_token": csrf},
            follow_redirects=False,
        )
        assert logout.status_code == 303
        restarted.cookies.set(SESSION_COOKIE, token, path="/")
        replay = restarted.get("/", follow_redirects=False)
        assert replay.status_code == 303
        assert replay.headers["location"].startswith("/auth/login")


@dataclass
class MutableClock:
    value: datetime

    def __call__(self) -> datetime:
        return self.value


def test_session_idle_expiry_and_stable_provider_subject(tmp_path):
    clock = MutableClock(datetime(2026, 8, 27, 13, 0, tzinfo=timezone.utc))
    store = WorkspaceStore(tmp_path / "runtime/workbench.sqlite", clock=clock)
    identity = IdentityService(
        store,
        tmp_path / "runtime",
        auth_mode="preview",
        clock=clock,
        idle_timeout=timedelta(minutes=5),
        absolute_timeout=timedelta(hours=1),
    )
    context, token = identity.login_preview("taylor-morgan")
    original_id = context.principal_id
    renamed = store.upsert_principal(
        "preview",
        "taylor-morgan",
        "Taylor Morgan-Smith",
        "taylor.morgan-smith@example.test",
    )
    assert renamed.principal_id == original_id

    clock.value += timedelta(minutes=6)
    assert identity.resolve(token) is None
    assert store.session_by_id(context.session.session_id).revoked_at is not None
    store.close()


def test_login_redirect_is_local_and_unconfigured_domain_mode_fails_closed(tmp_path):
    with TestClient(
        create_workbench_app(
            tmp_path / "preview",
            generator=UnavailableGenerator(),
            auth_mode="preview",
            answer_workers=1,
        )
    ) as client:
        page = client.get("/auth/login", params={"next": "//example.invalid/steal"})
        assert 'name="next" value="/"' in page.text
        challenge = client.cookies.get(LOGIN_CHALLENGE_COOKIE)
        response = client.post(
            "/auth/login",
            data={
                "identity_subject": "taylor-morgan",
                "login_challenge": challenge,
                "next": "https://example.invalid/steal",
            },
            follow_redirects=False,
        )
        assert response.headers["location"] == "/"

    with pytest.raises(RuntimeError, match="OIDC client secret file is required"):
        create_workbench_app(
            tmp_path / "oidc",
            generator=UnavailableGenerator(),
            auth_mode="oidc",
            secure_cookie=True,
            answer_workers=1,
        )


def test_revoked_member_cannot_queue_or_execute_saved_answer(tmp_path):
    bench = CaseIntelligenceWorkbench(
        tmp_path / "runtime",
        generator=UnavailableGenerator(),
        answer_workers=1,
    )
    assert bench.answers is not None
    bench.answers.close()
    bench.answers = None
    owner = bench.workspace.upsert_principal(
        "test", "owner", "Owner User", "owner", preferred_principal_id="principal-owner"
    )
    member = bench.workspace.upsert_principal(
        "test", "member", "Member User", "member", preferred_principal_id="principal-member"
    )
    matter = bench.create_matter("Revocation fixture", "Synthetic", owner.principal_id)
    bench.workspace.add_member(matter.matter_id, member.principal_id, owner.principal_id)
    conversation = bench.workspace.get_conversation(matter.matter_id)
    second_conversation = bench.workspace.create_conversation(matter.matter_id)
    job, _created = bench.workspace.queue_answer_job(
        matter.matter_id,
        conversation.conversation_id,
        member.principal_id,
        "What is supported?",
        "answer-request-" + "a" * 32,
    )
    bench.workspace.revoke_member(matter.matter_id, member.principal_id, owner.principal_id)
    with pytest.raises(AnswerJobFailure, match="Access to this matter was removed"):
        bench._process_answer_job(job, lambda _stage, _message: None, lambda: False)
    with pytest.raises(KeyError):
        bench.workspace.queue_answer_job(
            matter.matter_id,
            second_conversation.conversation_id,
            member.principal_id,
            "Can this still run?",
            "answer-request-" + "b" * 32,
        )
    bench.close()


def test_every_authenticated_mutating_route_has_csrf_dependency(tmp_path):
    app = create_workbench_app(
        tmp_path / "runtime",
        generator=UnavailableGenerator(),
        auth_mode="test",
        answer_workers=1,
    )
    try:
        missing = []
        for route in app.routes:
            methods = getattr(route, "methods", set())
            if (
                not methods.intersection({"POST", "PUT", "PATCH", "DELETE"})
                or route.path in {"/auth/login", "/auth/local"}
            ):
                continue
            dependencies = getattr(getattr(route, "dependant", None), "dependencies", ())
            names = {getattr(item.call, "__name__", "") for item in dependencies}
            if not names.intersection({"require_csrf", "require_csrf_header"}):
                missing.append(route.path)
        assert missing == []
    finally:
        app.state.workbench.close()


def test_identity_migration_matches_operator_copy():
    root = Path(__file__).parents[1]
    assert (
        root / "src/case_intelligence/migrations/sqlite/0005_identity_membership_audit.sql"
    ).read_bytes() == (
        root / "migrations/sqlite/0005_identity_membership_audit.sql"
    ).read_bytes()
