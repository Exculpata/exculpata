from __future__ import annotations

from dataclasses import replace

from fastapi.testclient import TestClient

from case_intelligence.generation import UnavailableGenerator
from case_intelligence.identity import LocalAccountSettings
from case_intelligence.workbench import create_workbench_app
from tests.test_browser_local_accounts import ORIGIN, PASSWORD, configured_app, create, csrf, login
from tests.test_local_authentication import _challenge
from tests.test_processing_awareness import _catalog_source


def test_setup_progress_uses_sources_membership_and_latest_grant_access(tmp_path):
    app, repo = configured_app(tmp_path)
    with TestClient(app, base_url=ORIGIN) as admin:
        assert login(admin).headers["location"] == "/admin/setup"
        page = admin.get("/admin/setup")
        assert "Bring your team into Exculpata" in page.text
        assert "No active matter in your case team" in page.text
        assert "temporarily unavailable" in page.text
        assert create(admin, "first.reviewer").status_code == 303
        assert create(admin, "second.reviewer").status_code == 303
        first, second = TestClient(app, base_url=ORIGIN), TestClient(app, base_url=ORIGIN)
        login(first, "first.reviewer")
        login(second, "second.reviewer")
        assert first.get("/admin/setup").status_code == 403
        assert "Another eligible person has signed in" in admin.get("/admin/setup").text
        response = admin.post("/matters", data={"csrf_token": csrf(admin), "name": "Synthetic onboarding matter"}, follow_redirects=False)
        slug = response.headers["location"].split("/")[2]
        workspace = app.state.workbench.workspace
        matter = workspace.all_matters()[0]
        workspace.upsert_source_catalog(matter.matter_id, [_catalog_source("a" * 32, tone="ready", state="ready")])
        page = admin.get("/admin/setup", params={"matter": slug})
        assert "1 of 1 sources are searchable" in page.text
        assert f'/matters/{slug}/setup#case-team-heading' in page.text
        principal = next(p for p in workspace.active_principals() if p.provider_subject == "first.reviewer")
        admin.post(f"/matters/{slug}/members", data={"csrf_token": csrf(admin), "principal_id": principal.principal_id})
        assert "Waiting for a current teammate to open" in admin.get("/admin/setup").text
        assert second.get(f"/matters/{slug}/home").status_code == 404
        assert first.get(f"/matters/{slug}/home").status_code == 200
        assert "A current teammate has opened this matter" in admin.get("/admin/setup").text
        admin.post(f"/matters/{slug}/members/{principal.principal_id}/remove", data={"csrf_token": csrf(admin)})
        admin.post(f"/matters/{slug}/members", data={"csrf_token": csrf(admin), "principal_id": principal.principal_id})
        assert "Waiting for a current teammate to open" in admin.get("/admin/setup").text
        first.close()
        second.close()
    # A restart reconciles this synthetic projection, which has no stored source.
    # Team progress remains derived from persisted grants and access events.
    restarted = create_workbench_app(tmp_path / "runtime", generator=UnavailableGenerator(), auth_mode="local",
        secure_cookie=True, local_settings=LocalAccountSettings(repo.path, management_root=repo.path.parent), learned_retrieval=False, answer_workers=1)
    with TestClient(restarted, base_url=ORIGIN) as admin:
        login(admin)
        page = admin.get("/admin/setup", params={"matter": slug})
        assert "0 of 0 sources are searchable" in page.text
        assert "Another eligible person is on this case team" in page.text
        assert "Waiting for a current teammate to open" in page.text


def test_oidc_setup_explains_provider_admission_and_matter_access(tmp_path):
    from tests.test_oidc_authentication import _settings, _begin, FakeOidcProvider
    settings = _settings(allowed_groups=frozenset({"review-users"}), administrator_groups=frozenset({"review-administrators"}))
    provider = FakeOidcProvider(settings)
    provider.groups = ["review-users", "review-administrators"]
    app = create_workbench_app(tmp_path / "runtime", generator=UnavailableGenerator(), auth_mode="oidc", secure_cookie=True,
        oidc_settings=settings, oidc_client=provider, learned_retrieval=False, answer_workers=1)
    with TestClient(app, base_url=settings.external_origin) as client:
        state = _begin(client, "/")
        response = client.get("/auth/oidc/callback", params={"code": "synthetic-administrator", "state": state})
        assert response.url.path == "/admin/setup"
        assert "sign-in provider manages accounts, admission groups and administrator roles" in response.text
        assert "Application administrator access and matter-team membership are separate" in response.text
        assert "Windows directory manages" not in response.text


def test_kerberos_setup_keeps_trusted_proxy_and_directory_copy(tmp_path):
    from tests.test_kerberos_authentication import _settings, _profile, _headers, REVIEWER_PRINCIPAL
    settings = replace(_settings(), administrator_principals=frozenset({REVIEWER_PRINCIPAL.casefold()}))
    app = create_workbench_app(tmp_path / "runtime", generator=UnavailableGenerator(), auth_mode="kerberos", secure_cookie=True,
        kerberos_settings=settings, kerberos_profile_resolver=_profile, learned_retrieval=False, answer_workers=1)
    with TestClient(app, base_url=ORIGIN) as client:
        assert client.get("/admin/setup").status_code == 401
        response = client.get("/auth/login", headers=_headers(), params={"next": "/"})
        assert response.url.path == "/admin/setup"
        assert "Windows directory manages accounts, admission groups and administrator groups" in response.text
        assert "Directory admission does not grant matter access" in response.text


def test_setup_preserves_explicit_destination_and_refuses_unjoined_matter(tmp_path):
    app, repo = configured_app(tmp_path)
    with TestClient(app, base_url=ORIGIN) as admin:
        page = admin.get("/auth/login")
        response = admin.post("/auth/local", data={
            "username": "alice.admin", "password": PASSWORD,
            "login_challenge": _challenge(page.text), "next": "/admin",
        }, follow_redirects=False)
        assert response.headers["location"] == "/admin"
        workspace = app.state.workbench.workspace
        other = workspace.upsert_principal("test", "other.owner", "Other owner", "other.owner")
        foreign = workspace.create_matter("Separate synthetic matter", "", other.principal_id)
        response = admin.get("/admin/setup", params={"matter": foreign.slug})
        assert response.status_code == 404
        assert workspace.members(foreign.matter_id)[0].principal_id == other.principal_id
        assert len(workspace.members(foreign.matter_id)) == 1


def test_setup_does_not_count_disabled_local_teammates_or_assume_model_readiness(tmp_path):
    app, repo = configured_app(tmp_path)
    with TestClient(app, base_url=ORIGIN) as admin:
        login(admin)
        create(admin, "first.reviewer")
        with TestClient(app, base_url=ORIGIN) as reviewer:
            login(reviewer, "first.reviewer")
            response = admin.post("/matters", data={
                "csrf_token": csrf(admin), "name": "Synthetic setup membership",
            }, follow_redirects=False)
            slug = response.headers["location"].split("/")[2]
            workspace = app.state.workbench.workspace
            person = next(p for p in workspace.active_principals() if p.provider_subject == "first.reviewer")
            admin.post(f"/matters/{slug}/members", data={"csrf_token": csrf(admin), "principal_id": person.principal_id})
            assert reviewer.get(f"/matters/{slug}/home").status_code == 200
            assert "A current teammate has opened this matter" in admin.get("/admin/setup").text
            repo.set_enabled("first.reviewer", False, actor="synthetic-operator")
            page = admin.get("/admin/setup")
            assert "Waiting for a current teammate to open" in page.text
            assert "Waiting for another eligible person to sign in" in page.text
            assert "Optional answers: temporarily unavailable" in page.text
            assert page.headers["cache-control"] == "no-store"


def test_seeded_preview_identities_do_not_claim_observed_signin(tmp_path, monkeypatch):
    app = create_workbench_app(tmp_path / 'runtime', generator=UnavailableGenerator(), auth_mode='test', learned_retrieval=False)
    identity = app.state.identity
    original = identity.resolve
    monkeypatch.setattr(identity, 'resolve', lambda token: replace(original(token), application_roles=frozenset({'administrator'})))
    with TestClient(app) as client:
        page = client.get('/admin/setup')
        assert page.status_code == 200
        assert 'Another eligible person has signed in' not in page.text
        assert 'Preview identities are available; teammate sign-in is not verified.' in page.text


def test_people_recovery_guidance_uses_installed_release_documents(tmp_path):
    app, repo = configured_app(tmp_path, enabled=False)
    with TestClient(app, base_url=ORIGIN) as admin:
        login(admin)
        page = admin.get("/admin/people/setup")
        assert page.status_code == 200
        assert "reviewed release currently running" in page.text
        assert "docs/LOCAL_ACCOUNT_BROWSER.md" in page.text
        assert "blob/main" not in page.text


def test_people_qa_clears_deployment_settings_before_runtime_import(monkeypatch, tmp_path):
    import runpy
    import os
    namespace = runpy.run_path(str(__import__('pathlib').Path(__file__).parents[1] / 'scripts/qa-people-browser.py'))
    monkeypatch.setattr(os, "environ", os.environ.copy())
    for key in ("CASE_INTELLIGENCE_POSTGRES_DSN", "CASE_INTELLIGENCE_MANAGED_STORAGE_ROOT", "CASE_INTELLIGENCE_SOURCE_REGISTRY", "CASE_INTELLIGENCE_TRANSCRIPTION_URL", "CASE_INTELLIGENCE_CLAMAV_HOST", "RECORDBENCH_LOCAL_ACCOUNT_ROOT"):
        monkeypatch.setenv(key, "synthetic-inherited-value")
    monkeypatch.setenv("PLAYWRIGHT_MODULE", "synthetic-browser-package")
    monkeypatch.setenv("RECORDBENCH_QA_BROWSER_CHANNEL", "chrome")
    namespace["_isolate_environment"]()
    assert not any(key.startswith("CASE_INTELLIGENCE_") for key in os.environ)
    assert "RECORDBENCH_LOCAL_ACCOUNT_ROOT" not in os.environ
    assert os.environ["PLAYWRIGHT_MODULE"] == "synthetic-browser-package"
    assert os.environ["RECORDBENCH_QA_BROWSER_CHANNEL"] == "chrome"
