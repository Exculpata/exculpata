from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from case_intelligence.generation import UnavailableGenerator
from case_intelligence.identity import (
    KERBEROS_SECRET_HEADER,
    KERBEROS_USER_HEADER,
    SESSION_COOKIE,
    KerberosSettings,
)
from case_intelligence.workbench import create_workbench_app


PROXY_SECRET = "synthetic_recordbench_proxy_secret_0000000000000000"
REALM = "EXAMPLE.TEST"
ADMIN = "alice.reviewer@EXAMPLE.TEST"
OWNER = "case.owner@EXAMPLE.TEST"
OTHER = "ordinary.reviewer@EXAMPLE.TEST"
USER_GROUP = "recordbench_users@example.test"
ADMIN_GROUP = "recordbench_administrators@example.test"


def _headers(principal: str) -> dict[str, str]:
    return {
        KERBEROS_USER_HEADER: principal,
        KERBEROS_SECRET_HEADER: PROXY_SECRET,
    }


def _profile(principal: str) -> tuple[str, str]:
    local = principal.split("@", 1)[0]
    display = " ".join(part.capitalize() for part in local.split("."))
    return display, principal.casefold()


def _csrf(page: str) -> str:
    matched = re.search(r'data-csrf-token="([0-9a-f]{64})"', page)
    assert matched is not None
    return matched.group(1)


def _settings(
    *,
    allowed_principals: frozenset[str] = frozenset(),
    administrator_principals: frozenset[str] = frozenset(),
) -> KerberosSettings:
    return KerberosSettings(
        realm=REALM,
        proxy_secret=PROXY_SECRET,
        allowed_principals=allowed_principals,
        allowed_groups=frozenset({USER_GROUP}),
        administrator_principals=administrator_principals,
        administrator_groups=frozenset({ADMIN_GROUP}),
    )


def test_group_admission_assigns_admin_and_rechecks_membership(tmp_path):
    memberships = {ADMIN: {ADMIN_GROUP}}

    app = create_workbench_app(
        tmp_path / "runtime",
        generator=UnavailableGenerator(),
        auth_mode="kerberos",
        secure_cookie=True,
        kerberos_settings=_settings(),
        kerberos_profile_resolver=_profile,
        kerberos_group_resolver=lambda principal: memberships.get(principal, set()),
        answer_workers=1,
    )
    with TestClient(app, base_url="https://recordbench.example.test") as client:
        login = client.get(
            "/auth/login",
            headers=_headers(ADMIN),
            follow_redirects=False,
        )
        assert login.status_code == 303

        landing = client.get("/matters/new", headers=_headers(ADMIN))
        assert landing.status_code == 200
        assert "Administrator" in landing.text
        assert "/admin" in landing.text
        session_token = client.cookies.get(SESSION_COOKIE)
        assert session_token

        memberships[ADMIN] = set()
        removed = client.get(
            "/matters/new",
            headers=_headers(ADMIN),
            follow_redirects=False,
        )
        assert removed.status_code == 303
        assert removed.headers["location"].startswith("/auth/login")
        session = app.state.workbench.workspace.connection.execute(
            "SELECT revoked_at FROM workbench_session WHERE token_digest=?",
            (app.state.identity.token_digest(session_token),),
        ).fetchone()
        assert session is not None and session[0] is not None


def test_exact_bootstrap_admin_survives_directory_group_lookup_failure(tmp_path):
    def unavailable(_principal: str):
        raise OSError("synthetic directory outage")

    app = create_workbench_app(
        tmp_path / "runtime",
        generator=UnavailableGenerator(),
        auth_mode="kerberos",
        secure_cookie=True,
        kerberos_settings=_settings(
            allowed_principals=frozenset({ADMIN}),
            administrator_principals=frozenset({ADMIN}),
        ),
        kerberos_profile_resolver=_profile,
        kerberos_group_resolver=unavailable,
        answer_workers=1,
    )
    with TestClient(app, base_url="https://recordbench.example.test") as client:
        login = client.get(
            "/auth/login",
            headers=_headers(ADMIN),
            follow_redirects=False,
        )
        assert login.status_code == 303
        page = client.get("/matters/new", headers=_headers(ADMIN))
        assert page.status_code == 200
        assert "Administrator" in page.text


def test_administrator_can_audit_view_manage_membership_and_reach_safe_close(
    tmp_path, monkeypatch
):
    backup_status = tmp_path / "recordbench-backup-status.json"
    backup_status.write_text(
        json.dumps(
            {
                "format_version": 1,
                "state": "succeeded",
                "message": "Encrypted NAS backup and repository verification completed.",
                "started_at": datetime.now(timezone.utc).isoformat(),
                "completed_at": datetime.now(timezone.utc).isoformat(),
                "duration_seconds": 19,
                "snapshot_id": "a" * 64,
                "repository": "/generated/not-rendered",
                "retention": "14d",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("RECORDBENCH_BACKUP_STATUS_FILE", str(backup_status))
    memberships = {
        ADMIN: {ADMIN_GROUP},
        OWNER: {USER_GROUP},
    }
    app = create_workbench_app(
        tmp_path / "runtime",
        generator=UnavailableGenerator(),
        auth_mode="kerberos",
        secure_cookie=True,
        kerberos_settings=_settings(),
        kerberos_profile_resolver=_profile,
        kerberos_group_resolver=lambda principal: memberships.get(principal, set()),
        answer_workers=1,
    )
    with TestClient(app, base_url="https://recordbench.example.test") as client:
        owner_login = client.get(
            "/auth/login",
            headers=_headers(OWNER),
            follow_redirects=False,
        )
        assert owner_login.status_code == 303
        owner_page = client.get("/matters/new", headers=_headers(OWNER))
        owner_csrf = _csrf(owner_page.text)
        created = client.post(
            "/matters",
            data={
                "csrf_token": owner_csrf,
                "name": "Synthetic administrator boundary",
                "descriptor": "Generated authorization fixture",
            },
            headers=_headers(OWNER),
            follow_redirects=False,
        )
        assert created.status_code == 303
        slug = created.headers["location"].split("/")[2]

        client.cookies.clear()
        admin_login = client.get(
            "/auth/login",
            headers=_headers(ADMIN),
            follow_redirects=False,
        )
        assert admin_login.status_code == 303

        console = client.get("/admin", headers=_headers(ADMIN))
        assert console.status_code == 200
        assert "Synthetic administrator boundary" in console.text
        assert "Case Owner" in console.text
        admin_csrf = _csrf(console.text)
        assert "Temporary workspace lifecycle" in console.text
        assert "Disaster recovery" in console.text
        assert "Local AI portfolio" in console.text
        assert "Focused answer" in console.text
        assert "Broader investigation" in console.text
        assert "Every-source check" in console.text
        assert "Switch model" not in console.text
        assert "Protected and current" in console.text
        assert "Encrypted NAS backup current" in console.text
        assert "Snapshot receipt" in console.text
        assert "aaaaaaaaaaaa" not in console.text
        assert "Qwen" not in console.text
        assert "vLLM" not in console.text
        assert "suite fingerprint" not in console.text
        assert "/generated/not-rendered" not in console.text

        expiry = (datetime.now(timezone.utc) + timedelta(days=45)).date().isoformat()
        rescheduled = client.post(
            f"/admin/matters/{slug}/retention",
            data={"csrf_token": admin_csrf, "expires_on": expiry},
            headers=_headers(ADMIN),
            follow_redirects=False,
        )
        assert rescheduled.status_code == 303
        cleaned = client.post(
            "/admin/maintenance/uploads",
            data={"csrf_token": admin_csrf},
            headers=_headers(ADMIN),
            follow_redirects=False,
        )
        assert cleaned.status_code == 303

        opened = client.get(f"/matters/{slug}", headers=_headers(ADMIN))
        assert opened.status_code == 200
        assert "Administrator view" in opened.text
        assert "read-only" in opened.text

        setup = client.get(f"/matters/{slug}/setup", headers=_headers(ADMIN))
        assert setup.status_code == 200
        assert "Administrator view" in setup.text

        notebook = client.get(f"/matters/{slug}/notebook", headers=_headers(ADMIN))
        assert notebook.status_code == 200
        assert "Administrator view" in notebook.text

        blocked_write = client.post(
            f"/matters/{slug}/conversations",
            data={"csrf_token": admin_csrf},
            headers=_headers(ADMIN),
        )
        assert blocked_write.status_code == 403

        admin_principal = app.state.workbench.workspace.connection.execute(
            "SELECT principal_id FROM workbench_principal WHERE provider_subject=?",
            (ADMIN,),
        ).fetchone()
        assert admin_principal is not None
        added = client.post(
            f"/matters/{slug}/members",
            data={
                "csrf_token": admin_csrf,
                "principal_id": admin_principal[0],
            },
            headers=_headers(ADMIN),
            follow_redirects=False,
        )
        assert added.status_code == 303
        assert app.state.workbench.workspace.membership(
            app.state.workbench.workspace.get_active_matter(slug).matter_id,
            admin_principal[0],
        ).role == "member"

        close_page = client.get(
            f"/matters/{slug}/close",
            headers=_headers(ADMIN),
        )
        assert close_page.status_code == 200
        assert "Administrator deletion" in close_page.text
        assert "using administrator authority" in close_page.text
        assert "Download final bundle" in close_page.text

        audit = app.state.workbench.workspace.connection.execute(
            "SELECT action,outcome,details_json FROM workbench_audit_event "
            "WHERE action IN ('matter.admin_access','membership.add') "
            "ORDER BY occurred_at,event_id"
        ).fetchall()
        assert any(row[0:2] == ("matter.admin_access", "success") for row in audit)
        assert any(row[0:2] == ("membership.add", "success") for row in audit)
        assert all("administrator" in row[2] or row[0] == "membership.add" for row in audit)


def test_regular_group_user_cannot_use_administrator_console(tmp_path):
    app = create_workbench_app(
        tmp_path / "runtime",
        generator=UnavailableGenerator(),
        auth_mode="kerberos",
        secure_cookie=True,
        kerberos_settings=_settings(),
        kerberos_profile_resolver=_profile,
        kerberos_group_resolver=lambda _principal: {USER_GROUP},
        answer_workers=1,
    )
    with TestClient(app, base_url="https://recordbench.example.test") as client:
        assert client.get(
            "/auth/login",
            headers=_headers(OWNER),
            follow_redirects=False,
        ).status_code == 303
        page = client.get("/matters/new", headers=_headers(OWNER))
        assert page.status_code == 200
        assert "/admin" not in page.text
        denied = client.get("/admin", headers=_headers(OWNER))
        assert denied.status_code == 403


def test_kerberos_logout_is_labelled_as_application_session_close(tmp_path):
    app = create_workbench_app(
        tmp_path / "runtime",
        generator=UnavailableGenerator(),
        auth_mode="kerberos",
        secure_cookie=True,
        kerberos_settings=_settings(allowed_principals=frozenset({ADMIN})),
        kerberos_profile_resolver=_profile,
        kerberos_group_resolver=lambda _principal: set(),
        answer_workers=1,
    )
    with TestClient(app, base_url="https://recordbench.example.test") as client:
        assert client.get(
            "/auth/login",
            headers=_headers(ADMIN),
            follow_redirects=False,
        ).status_code == 303
        page = client.get("/matters/new", headers=_headers(ADMIN))
        assert "Close Exculpata" in page.text
        closed = client.post(
            "/auth/logout",
            data={"csrf_token": _csrf(page.text)},
            headers=_headers(ADMIN),
            follow_redirects=False,
        )
        assert closed.status_code == 303
        signed_out = client.get(closed.headers["location"], headers=_headers(ADMIN))
        assert "Windows remains signed in" in signed_out.text
        assert "current Windows user" in signed_out.text


def test_kerberos_group_policy_rejects_empty_or_malformed_configuration():
    with pytest.raises(RuntimeError, match="admission policy"):
        KerberosSettings(realm=REALM, proxy_secret=PROXY_SECRET)
    with pytest.raises(RuntimeError, match="group configuration"):
        KerberosSettings(
            realm=REALM,
            proxy_secret=PROXY_SECRET,
            allowed_groups=frozenset({"RecordBench,Users"}),
        )


def test_ordinary_user_report_analysis_isolation_and_group_revocation(tmp_path):
    memberships = {OWNER: {USER_GROUP}, OTHER: {USER_GROUP}}
    app = create_workbench_app(
        tmp_path / "runtime",
        generator=UnavailableGenerator(),
        auth_mode="kerberos",
        secure_cookie=True,
        kerberos_settings=_settings(),
        kerberos_profile_resolver=_profile,
        kerberos_group_resolver=lambda principal: memberships.get(principal, set()),
        answer_workers=1,
    )
    with TestClient(app, base_url="https://recordbench.example.test") as client:
        assert client.get(
            "/auth/login", headers=_headers(OWNER), follow_redirects=False
        ).status_code == 303
        owner_page = client.get("/matters/new", headers=_headers(OWNER))
        owner_csrf = _csrf(owner_page.text)
        created = client.post(
            "/matters",
            data={
                "csrf_token": owner_csrf,
                "name": "Synthetic owner-only review tools",
                "descriptor": "Generated authorization fixture",
            },
            headers=_headers(OWNER),
            follow_redirects=False,
        )
        slug = created.headers["location"].split("/")[2]
        report = client.post(
            f"/matters/{slug}/reports",
            data={
                "csrf_token": owner_csrf,
                "title": "Owner-only draft",
                "purpose": "Generated access test",
            },
            headers=_headers(OWNER),
            follow_redirects=False,
        )
        assert report.status_code == 303

        client.cookies.clear()
        assert client.get(
            "/auth/login", headers=_headers(OTHER), follow_redirects=False
        ).status_code == 303
        other_page = client.get("/matters/new", headers=_headers(OTHER))
        other_csrf = _csrf(other_page.text)
        assert client.get(f"/matters/{slug}/reports", headers=_headers(OTHER)).status_code == 404
        assert client.get(f"/matters/{slug}/analysis", headers=_headers(OTHER)).status_code == 404
        denied_write = client.post(
            f"/matters/{slug}/reports",
            data={"csrf_token": other_csrf, "title": "Crossed draft"},
            headers=_headers(OTHER),
        )
        assert denied_write.status_code == 404

        session_token = client.cookies.get(SESSION_COOKIE)
        assert session_token
        memberships[OTHER] = set()
        revoked = client.get(
            "/matters/new", headers=_headers(OTHER), follow_redirects=False
        )
        assert revoked.status_code == 303
        assert revoked.headers["location"].startswith("/auth/login")
        session = app.state.workbench.workspace.connection.execute(
            "SELECT revoked_at FROM workbench_session WHERE token_digest=?",
            (app.state.identity.token_digest(session_token),),
        ).fetchone()
        assert session is not None and session[0] is not None
