from __future__ import annotations


from case_intelligence.legacy_names import (
    LEGACY_STEM,
)
import re
import json
import shutil
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from case_intelligence.generation import UnavailableGenerator
from case_intelligence.identity import LocalAccountSettings
from case_intelligence.local_accounts import LocalAccountRepository
from case_intelligence.workbench import create_workbench_app
from tests.test_local_authentication import _challenge

PASSWORD = "synthetic-browser-password"
ORIGIN = f"https://{LEGACY_STEM}.example.test"


def configured_app(tmp_path, *, enabled=True):
    root = tmp_path / "accounts"
    repo = LocalAccountRepository(root / "local-accounts.json")
    repo.initialize("alice.admin", "Alice Administrator", PASSWORD, actor="synthetic-operator")
    settings = LocalAccountSettings(repo.path, management_root=root if enabled else None)
    app = create_workbench_app(tmp_path / "runtime", generator=UnavailableGenerator(),
                              auth_mode="local", secure_cookie=True, local_settings=settings,
                              learned_retrieval=False, answer_workers=1)
    return app, repo


def login(client, username="alice.admin", password=PASSWORD):
    page = client.get("/auth/login")
    return client.post("/auth/local", data={"username": username, "password": password,
                       "login_challenge": _challenge(page.text), "next": "/"}, follow_redirects=False)


def token(page, name):
    match = re.search(r'name="' + name + r'" value="([^"]+)"', page.text)
    assert match
    return match.group(1)


def csrf(client):
    return token(client.get("/admin/people"), "csrf_token")


def create(client, username, *, role="reviewer"):
    return client.post("/admin/people/create", data={"csrf_token": csrf(client), "username": username,
                       "display_name": username.replace(".", " ").title(), "password": PASSWORD,
                       "password_confirm": PASSWORD, "role": role}, follow_redirects=False)


def edit(client, username, action, **fields):
    page = client.get("/admin/people/accounts/" + username)
    return client.post(f"/admin/people/accounts/{username}/{action}",
                       data={"csrf_token": token(page, "csrf_token"), "edit_token": token(page, "edit_token"), **fields},
                       follow_redirects=False)


def test_first_administrator_can_create_people_and_control_two_user_access(tmp_path):
    app, repo = configured_app(tmp_path)
    with TestClient(app, base_url=ORIGIN) as admin:
        assert login(admin).headers["location"] == "/admin/setup"
        page = admin.get("/admin/people")
        assert "Add your first teammate" in page.text and "Create account" in page.text
        assert create(admin, "first.reviewer").status_code == 303
        assert create(admin, "second.reviewer").status_code == 303
        first, second = TestClient(app, base_url=ORIGIN), TestClient(app, base_url=ORIGIN)
        assert login(first, "first.reviewer").status_code == 303
        assert login(second, "second.reviewer").status_code == 303
        response = admin.post("/matters", data={"csrf_token": csrf(admin), "name": "Synthetic restricted matter", "descriptor": ""}, follow_redirects=False)
        slug = response.headers["location"].split("/")[2]
        principal = next(p for p in app.state.workbench.workspace.active_principals() if p.provider_subject == "first.reviewer")
        assert admin.post(f"/matters/{slug}/members", data={"csrf_token": csrf(admin), "principal_id": principal.principal_id}, follow_redirects=False).status_code == 303
        assert first.get(f"/matters/{slug}").status_code == 200
        assert second.get(f"/matters/{slug}").status_code in {403, 404}
        assert second.get("/admin/people").status_code == 403
        assert edit(admin, "first.reviewer", "password", password="synthetic-reset-password", password_confirm="synthetic-reset-password").status_code == 303
        assert first.get(f"/matters/{slug}", follow_redirects=False).headers["location"].startswith("/auth/login")
        assert login(first, "first.reviewer", "synthetic-reset-password").status_code == 303
        assert first.get(f"/matters/{slug}").status_code == 200
        assert edit(admin, "first.reviewer", "state", enabled="no").status_code == 303
        assert first.get(f"/matters/{slug}", follow_redirects=False).headers["location"].startswith("/auth/login")
        response_text = admin.get("/admin/people").text
        assert PASSWORD not in response_text and "synthetic-reset-password" not in response_text
        assert all(account.password_hash not in response_text for account in repo.read().values())
        events = app.state.workbench.workspace.audit_events()
        account_events = [event for event in events if event.action.startswith("account.")]
        assert account_events and all(event.actor_principal_id for event in account_events)
        assert PASSWORD not in str(account_events) and "$argon2" not in str(account_events)
        first.close()
        second.close()


def test_csrf_disabled_mode_and_ordinary_accounts_cannot_mutate(tmp_path):
    app, repo = configured_app(tmp_path, enabled=False)
    with TestClient(app, base_url=ORIGIN) as admin:
        login(admin)
        assert "Open operator setup instructions" in admin.get("/admin/people").text
        before = repo.path.read_bytes()
        assert create(admin, "not.created").status_code == 403
        assert repo.path.read_bytes() == before
    app, repo = configured_app(tmp_path / "enabled")
    with TestClient(app, base_url=ORIGIN) as admin:
        login(admin)
        before = repo.path.read_bytes()
        response = admin.post("/admin/people/create", data={"username": "blocked", "password": PASSWORD}, follow_redirects=False)
        assert response.status_code == 403 and repo.path.read_bytes() == before
        assert create(admin, "ordinary.user").status_code == 303
        other = TestClient(app, base_url=ORIGIN)
        login(other, "ordinary.user")
        assert other.post("/admin/people/create", data={"password": PASSWORD}).status_code == 403
        other.close()


def test_stale_edit_and_last_administrator_have_actionable_recovery(tmp_path):
    app, repo = configured_app(tmp_path)
    with TestClient(app, base_url=ORIGIN) as admin:
        login(admin)
        old = admin.get("/admin/people/accounts/alice.admin")
        repo.change_display_name("alice.admin", "Changed Elsewhere", actor="synthetic-operator")
        stale = admin.post("/admin/people/accounts/alice.admin/name", data={"csrf_token": token(old, "csrf_token"), "edit_token": token(old, "edit_token"), "display_name": "Stale Name"})
        assert stale.status_code == 409 and "another window" in stale.text
        assert repo.read()["alice.admin"].display_name == "Changed Elsewhere"
        removal = edit(admin, "alice.admin", "role", role="reviewer", confirm_self="yes")
        assert removal.status_code == 400 and "Keep one enabled administrator" in removal.text
        assert "administrator" in repo.read()["alice.admin"].roles


@pytest.mark.parametrize("password", ["synthetic-short", "X" * 1025, "X" * 40000])
def test_password_validation_never_echoes_input(tmp_path, password):
    app, repo = configured_app(tmp_path)
    with TestClient(app, base_url=ORIGIN) as admin:
        login(admin)
        response = admin.post("/admin/people/create", data={"csrf_token": csrf(admin), "username": "invalid.user", "display_name": "Generated User", "password": password, "password_confirm": "different-synthetic-value"})
        assert response.status_code in {400, 413}
        assert password not in response.text and "different-synthetic-value" not in response.text
        assert "invalid.user" not in repo.read()


@pytest.mark.parametrize("display_name", ["Synthetic\u202eReviewer", "Synthetic\x7fReviewer"])
@pytest.mark.parametrize("operation", ["create", "rename"])
def test_browser_rejects_unsupported_display_names_before_writing(tmp_path, display_name, operation):
    app, repo = configured_app(tmp_path)
    with TestClient(app, base_url=ORIGIN) as admin:
        login(admin)
        before = repo.path.read_bytes()
        if operation == "create":
            response = admin.post("/admin/people/create", data={"csrf_token": csrf(admin),
                "username": "new.reviewer", "display_name": display_name, "password": PASSWORD,
                "password_confirm": PASSWORD, "role": "reviewer"}, follow_redirects=False)
        else:
            response = edit(admin, "alice.admin", "name", display_name=display_name)
        assert response.status_code == 400
        assert "Display name" in response.text
        assert repo.path.read_bytes() == before
        assert admin.get("/admin/people").status_code == 200


@pytest.mark.parametrize("damage", ["extra-secret", "permissions", "symlink", "legacy"])
def test_management_boundary_rejects_unsafe_or_legacy_layout(tmp_path, damage):
    app, repo = configured_app(tmp_path)
    root = repo.path.parent
    if damage == "extra-secret":
        (root / "other-secret").write_text("synthetic secret")
    elif damage == "permissions":
        root.chmod(0o750)
    elif damage == "symlink":
        link = tmp_path / "linked-accounts"
        link.symlink_to(root, target_is_directory=True)
        root = link
    else:
        payload = json.loads(repo.path.read_text())
        payload["format_version"] = 1
        for account in payload["accounts"]:
            account.pop("session_revision")
        repo.path.write_text(json.dumps(payload))
    with pytest.raises((RuntimeError, OSError)):
        LocalAccountSettings(root / "local-accounts.json", management_root=root)


def test_relocation_has_verified_clean_restore_and_one_canonical_file(tmp_path):
    _, repo = configured_app(tmp_path)
    before = repo.path.read_bytes()
    destination = tmp_path / "dedicated" / "local-accounts.json"
    backup = tmp_path / "recovery" / "accounts.json"
    with pytest.raises(RuntimeError, match="Stop"):
        repo.relocate(destination, backup, actor="synthetic-operator")
    assert repo.path.read_bytes() == before
    repo.relocate(destination, backup, actor="synthetic-operator", writers_stopped=True)
    assert not repo.path.exists() and backup.read_bytes() != before
    moved = LocalAccountSettings(destination, management_root=destination.parent)
    restore = tmp_path / "clean-restore" / "local-accounts.json"
    restore.parent.mkdir(mode=0o700)
    shutil.copy2(backup, restore)
    restored = LocalAccountSettings(restore, management_root=restore.parent)
    assert restored.authenticate("alice.admin", PASSWORD) is not None
    assert moved.authenticate("alice.admin", PASSWORD) is not None
    assert restored.accounts["alice.admin"].session_revision != moved.accounts["alice.admin"].session_revision


def test_failed_relocation_backup_keeps_source(tmp_path):
    _, repo = configured_app(tmp_path)
    before = repo.path.read_bytes()
    backup = tmp_path / "existing-backup"
    backup.write_text("synthetic retained copy")
    with pytest.raises(FileExistsError):
        repo.relocate(tmp_path / "dedicated" / "local-accounts.json", backup,
                      actor="synthetic-operator", writers_stopped=True)
    assert repo.path.read_bytes() == before
    assert not (tmp_path / "dedicated" / "local-accounts.json").exists()


def test_relocation_rejects_nested_recovery_before_moving_source(tmp_path):
    _, repo = configured_app(tmp_path)
    before = repo.path.read_bytes()
    destination = tmp_path / "dedicated" / "local-accounts.json"
    with pytest.raises(RuntimeError, match="outside"):
        repo.relocate(destination, destination.parent / "nested/recovery.json", actor="synthetic-operator", writers_stopped=True)
    assert repo.path.read_bytes() == before and not destination.parent.exists()


def test_self_demotion_requires_confirmation_and_ends_session(tmp_path):
    app, repo = configured_app(tmp_path)
    with TestClient(app, base_url=ORIGIN) as admin:
        login(admin)
        assert create(admin, "second.admin", role="administrator").status_code == 303
        assert edit(admin, "alice.admin", "role", role="reviewer").status_code == 400
        assert "administrator" in repo.read()["alice.admin"].roles
        response = edit(admin, "alice.admin", "role", role="reviewer", confirm_self="yes")
        assert response.status_code == 303 and response.headers["location"].startswith("/auth/login")
        assert admin.get("/admin/people", follow_redirects=False).headers["location"].startswith("/auth/login")


def test_actor_revoked_after_handler_authorization_cannot_write(tmp_path, monkeypatch):
    from case_intelligence import local_account_admin
    app, repo = configured_app(tmp_path)
    with TestClient(app, base_url=ORIGIN) as admin:
        login(admin)
        assert create(admin, "second.admin", role="administrator").status_code == 303
        page = admin.get("/admin/people/accounts/second.admin")
        original = local_account_admin.run_in_threadpool
        async def revoked_then_apply(func, *args, **kwargs):
            repo.set_administrator("alice.admin", False, actor="synthetic-operator")
            return await original(func, *args, **kwargs)
        monkeypatch.setattr(local_account_admin, "run_in_threadpool", revoked_then_apply)
        response = admin.post("/admin/people/accounts/second.admin/name", data={
            "csrf_token": token(page, "csrf_token"), "edit_token": token(page, "edit_token"),
            "display_name": "Must not apply"})
        assert response.status_code == 403
        assert repo.read()["second.admin"].display_name != "Must not apply"


def test_audit_failure_prevents_mutation_and_missing_edit_token_conflicts(tmp_path, monkeypatch):
    app, repo = configured_app(tmp_path)
    with TestClient(app, base_url=ORIGIN) as admin:
        login(admin)
        before = repo.path.read_bytes()
        assert admin.post("/admin/people/accounts/alice.admin/name", data={
            "csrf_token": csrf(admin), "display_name": "Missing revision"}).status_code == 409
        original = app.state.workbench.workspace.append_audit_event
        def fail_requested(**event):
            if event["action"].endswith(".requested"):
                raise OSError("synthetic unavailable audit")
            return original(**event)
        monkeypatch.setattr(app.state.workbench.workspace, "append_audit_event", fail_requested)
        response = edit(admin, "alice.admin", "name", display_name="Must not apply")
        assert response.status_code == 503
        assert repo.path.read_bytes() == before


def test_self_change_reports_completion_audit_failure_at_sign_in(tmp_path, monkeypatch):
    app, repo = configured_app(tmp_path)
    with TestClient(app, base_url=ORIGIN) as admin:
        login(admin)
        original = app.state.workbench.workspace.append_audit_event
        def fail_completion(**event):
            if event["action"].endswith(".completed"):
                raise OSError("synthetic missing completion")
            return original(**event)
        monkeypatch.setattr(app.state.workbench.workspace, "append_audit_event", fail_completion)
        response = edit(admin, "alice.admin", "password", password="synthetic-new-password", password_confirm="synthetic-new-password")
        assert response.status_code == 303
        destination = urlsplit(response.headers["location"])
        assert destination.path == "/auth/login"
        assert "Audit completion needs operator attention" in parse_qs(destination.query)["error"][0]
        assert admin.get("/admin/people", follow_redirects=False).status_code == 303


@pytest.mark.parametrize("disabled", [False, True])
def test_browser_rename_refreshes_existing_offline_principal(tmp_path, disabled):
    app, repo = configured_app(tmp_path)
    with TestClient(app, base_url=ORIGIN) as admin:
        login(admin)
        assert create(admin, "offline.reviewer").status_code == 303
        reviewer = TestClient(app, base_url=ORIGIN)
        assert login(reviewer, "offline.reviewer").status_code == 303
        reviewer.close()
        store = app.state.workbench.workspace
        principal = next(p for p in store.active_principals() if p.provider_subject == "offline.reviewer")
        if disabled:
            assert edit(admin, "offline.reviewer", "state", enabled="no").status_code == 303
        before = store.get_principal(principal.principal_id)
        assert edit(admin, "offline.reviewer", "name", display_name="Renamed Offline Reviewer").status_code == 303
        refreshed = store.get_principal(principal.principal_id)
        assert refreshed.display_name == "Renamed Offline Reviewer"
        assert refreshed.principal_id == before.principal_id
        assert refreshed.active == before.active and refreshed.last_seen_at == before.last_seen_at
        assert repo.read()["offline.reviewer"].enabled is not disabled
        assert "Renamed Offline Reviewer" in admin.get("/admin").text


def test_browser_rename_does_not_create_an_unseen_principal(tmp_path):
    app, _ = configured_app(tmp_path)
    with TestClient(app, base_url=ORIGIN) as admin:
        login(admin)
        assert create(admin, "unseen.reviewer").status_code == 303
        assert edit(admin, "unseen.reviewer", "name", display_name="Unseen Renamed Reviewer").status_code == 303
        assert all(p.provider_subject != "unseen.reviewer" for p in app.state.workbench.workspace.active_principals())


def test_browser_account_audits_record_role_and_state_direction(tmp_path):
    app, _ = configured_app(tmp_path)
    with TestClient(app, base_url=ORIGIN) as admin:
        login(admin)
        assert create(admin, "target.reviewer").status_code == 303
        for route, fields, action in (("state", {"enabled": "no"}, "disable"),
                                      ("state", {"enabled": "yes"}, "enable"),
                                      ("role", {"role": "administrator"}, "administrator"),
                                      ("role", {"role": "reviewer"}, "reviewer")):
            assert edit(admin, "target.reviewer", route, **fields).status_code == 303
            events = app.state.workbench.workspace.audit_events()
            matching = [event for event in events if event.action.startswith(f"account.{action}.")]
            assert {event.action for event in matching} == {f"account.{action}.requested", f"account.{action}.completed"}
            assert len({event.actor_principal_id for event in matching}) == 1
            assert len({event.object_id for event in matching}) == 1
        assert all(not event.action.startswith(("account.state.", "account.role."))
                   for event in app.state.workbench.workspace.audit_events())


def test_projection_failure_reports_committed_rename_without_hiding_audit(tmp_path, monkeypatch):
    app, repo = configured_app(tmp_path)
    with TestClient(app, base_url=ORIGIN) as admin:
        login(admin)
        assert create(admin, "offline.reviewer").status_code == 303
        def unavailable(*args):
            raise OSError("synthetic principal projection failure")
        monkeypatch.setattr(app.state.workbench.workspace, "refresh_principal_display_name", unavailable)
        response = edit(admin, "offline.reviewer", "name", display_name="Saved Account Name")
        assert response.status_code == 303
        assert "Saved team names could not refresh" in parse_qs(urlsplit(response.headers["location"]).query)["notice"][0]
        assert repo.read()["offline.reviewer"].display_name == "Saved Account Name"
        assert any(event.action == "account.display-name.completed" for event in app.state.workbench.workspace.audit_events())
