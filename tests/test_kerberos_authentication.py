from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from case_intelligence.generation import UnavailableGenerator
from case_intelligence.identity import (
    KERBEROS_SECRET_HEADER,
    KERBEROS_USER_HEADER,
    SESSION_COOKIE,
    KerberosAuthenticationError,
    KerberosSettings,
)
from case_intelligence.workbench import create_workbench_app


PROXY_SECRET = "synthetic_recordbench_proxy_secret_0000000000000000"
REVIEWER_PRINCIPAL = "alice.reviewer@EXAMPLE.TEST"


def _settings(
    *, allowed_principals: frozenset[str] = frozenset({REVIEWER_PRINCIPAL})
) -> KerberosSettings:
    return KerberosSettings(
        realm="EXAMPLE.TEST",
        proxy_secret=PROXY_SECRET,
        allowed_principals=allowed_principals,
    )


def _headers(principal: str = REVIEWER_PRINCIPAL) -> dict[str, str]:
    return {
        KERBEROS_USER_HEADER: principal,
        KERBEROS_SECRET_HEADER: PROXY_SECRET,
    }


def _profile(principal: str) -> tuple[str, str]:
    assert principal == REVIEWER_PRINCIPAL
    return "Alice Reviewer", "alice.reviewer@example.test"


def test_kerberos_login_binds_windows_identity_to_server_session(tmp_path):
    app = create_workbench_app(
        tmp_path / "runtime",
        generator=UnavailableGenerator(),
        auth_mode="kerberos",
        secure_cookie=True,
        kerberos_settings=_settings(),
        kerberos_profile_resolver=_profile,
        answer_workers=1,
    )
    with TestClient(app, base_url="https://recordbench.example.test") as client:
        no_proxy_identity = client.get("/auth/login")
        assert no_proxy_identity.status_code == 401
        assert "Windows sign-in could not be verified" in no_proxy_identity.text
        assert "Taylor Morgan" not in no_proxy_identity.text

        forged_identity = client.get(
            "/auth/login",
            headers={KERBEROS_USER_HEADER: REVIEWER_PRINCIPAL},
        )
        assert forged_identity.status_code == 401
        assert client.cookies.get(SESSION_COOKIE) is None

        login = client.get(
            "/auth/login",
            params={"next": "/matters/new"},
            headers=_headers("alice.reviewer@example.test"),
            follow_redirects=False,
        )
        assert login.status_code == 303
        assert login.headers["location"] == "/matters/new"
        assert "Secure" in login.headers["set-cookie"]
        assert "HttpOnly" in login.headers["set-cookie"]
        session_token = client.cookies.get(SESSION_COOKIE)
        assert session_token

        principal_row = app.state.workbench.workspace.connection.execute(
            "SELECT principal_id,provider,provider_subject,display_name,login_name "
            "FROM workbench_principal WHERE provider_subject=?",
            (REVIEWER_PRINCIPAL,),
        ).fetchone()
        assert principal_row is not None
        assert principal_row[1] == _settings().provider_key
        assert principal_row[2:] == (
            REVIEWER_PRINCIPAL,
            "Alice Reviewer",
            "alice.reviewer@example.test",
        )

        landing = client.get("/matters/new", headers=_headers())
        assert landing.status_code == 200
        assert "signed in with your Windows domain account as Alice Reviewer" in landing.text
        assert "Organization account sign-in is not connected yet" not in landing.text
        csrf_match = re.search(r'data-csrf-token="([0-9a-f]{64})"', landing.text)
        assert csrf_match

        direct_cookie_replay = client.get("/matters/new", follow_redirects=False)
        assert direct_cookie_replay.status_code == 303
        assert direct_cookie_replay.headers["location"].startswith("/auth/login")

        different_windows_user = client.get(
            "/matters/new",
            headers=_headers("other.reviewer@EXAMPLE.TEST"),
            follow_redirects=False,
        )
        assert different_windows_user.status_code == 303
        assert different_windows_user.headers["location"].startswith("/auth/login")

        assert [
            principal.principal_id
            for principal in app.state.identity.membership_candidates()
        ] == [principal_row[0]]

        logout = client.post(
            "/auth/logout",
            data={"csrf_token": csrf_match.group(1)},
            headers=_headers(),
            follow_redirects=False,
        )
        assert logout.status_code == 303
        assert logout.headers["location"] == "/auth/signed-out"
        signed_out = client.get("/auth/signed-out", headers=_headers())
        assert signed_out.status_code == 200
        assert "Signed out of Exculpata" in signed_out.text

        client.cookies.set(SESSION_COOKIE, session_token, path="/")
        replay = client.get("/matters/new", headers=_headers(), follow_redirects=False)
        assert replay.status_code == 303
        assert replay.headers["location"].startswith("/auth/login")

        audit_rows = app.state.workbench.workspace.connection.execute(
            "SELECT action,outcome,details_json FROM workbench_audit_event "
            "ORDER BY occurred_at,event_id"
        ).fetchall()
        assert ("auth.login", "success") in {
            (row[0], row[1]) for row in audit_rows
        }
        assert ("auth.session_binding", "denied") in {
            (row[0], row[1]) for row in audit_rows
        }
        assert all(PROXY_SECRET not in row[2] for row in audit_rows)


def test_kerberos_rejects_unapproved_principal_without_provisioning(tmp_path):
    app = create_workbench_app(
        tmp_path / "runtime",
        generator=UnavailableGenerator(),
        auth_mode="kerberos",
        secure_cookie=True,
        kerberos_settings=_settings(),
        kerberos_profile_resolver=_profile,
        answer_workers=1,
    )
    with TestClient(app, base_url="https://recordbench.example.test") as client:
        denied = client.get(
            "/auth/login",
            headers=_headers("not.approved@EXAMPLE.TEST"),
        )
        assert denied.status_code == 401
        assert "not approved for Exculpata" in denied.text
        assert client.cookies.get(SESSION_COOKIE) is None
        count = app.state.workbench.workspace.connection.execute(
            "SELECT COUNT(*) FROM workbench_principal WHERE provider=?",
            (_settings().provider_key,),
        ).fetchone()[0]
        assert count == 0


def test_kerberos_env_requires_owner_only_secret_and_secure_cookie(
    tmp_path, monkeypatch
):
    secret_file = tmp_path / "proxy-secret"
    secret_file.write_text(PROXY_SECRET + "\n", encoding="ascii")
    secret_file.chmod(0o644)
    monkeypatch.setenv("CASE_INTELLIGENCE_AUTH_MODE", "kerberos")
    monkeypatch.setenv("CASE_INTELLIGENCE_SECURE_COOKIE", "1")
    monkeypatch.setenv("CASE_INTELLIGENCE_KERBEROS_REALM", "EXAMPLE.TEST")
    monkeypatch.setenv(
        "CASE_INTELLIGENCE_KERBEROS_ALLOWED_PRINCIPALS", REVIEWER_PRINCIPAL
    )
    monkeypatch.setenv(
        "CASE_INTELLIGENCE_KERBEROS_ALLOWED_GROUPS",
        "RecordBench_Users@EXAMPLE.TEST",
    )
    monkeypatch.setenv(
        "CASE_INTELLIGENCE_KERBEROS_ADMIN_PRINCIPALS", REVIEWER_PRINCIPAL
    )
    monkeypatch.setenv(
        "CASE_INTELLIGENCE_KERBEROS_ADMIN_GROUPS",
        "RecordBench_Administrators@EXAMPLE.TEST",
    )
    monkeypatch.setenv(
        "CASE_INTELLIGENCE_KERBEROS_PROXY_SECRET_FILE", str(secret_file)
    )

    with pytest.raises(RuntimeError, match="mode 0600"):
        create_workbench_app(
            tmp_path / "unsafe",
            generator=UnavailableGenerator(),
            answer_workers=1,
        )

    secret_file.chmod(0o600)
    app = create_workbench_app(
        tmp_path / "valid",
        generator=UnavailableGenerator(),
        kerberos_profile_resolver=_profile,
        answer_workers=1,
    )
    assert app.state.identity.kerberos_settings.allowed_principals == frozenset(
        {REVIEWER_PRINCIPAL}
    )
    assert app.state.identity.kerberos_settings.allowed_groups == frozenset(
        {"recordbench_users@example.test"}
    )
    assert app.state.identity.kerberos_settings.administrator_principals == frozenset(
        {REVIEWER_PRINCIPAL}
    )
    assert app.state.identity.kerberos_settings.administrator_groups == frozenset(
        {"recordbench_administrators@example.test"}
    )
    assert PROXY_SECRET not in repr(app.state.identity.kerberos_settings)
    app.state.workbench.close()

    with pytest.raises(RuntimeError, match="secure cookies"):
        create_workbench_app(
            tmp_path / "insecure",
            generator=UnavailableGenerator(),
            auth_mode="kerberos",
            secure_cookie=False,
            kerberos_settings=_settings(),
            answer_workers=1,
        )


@pytest.mark.parametrize(
    "principal",
    [
        "EXAMPLE\\alice.reviewer",
        "alice.reviewer@OTHER.EXAMPLE.TEST",
        "alice.reviewer@EXAMPLE.TEST@example.invalid",
        "comma,name@EXAMPLE.TEST",
    ],
)
def test_kerberos_principal_parser_fails_closed(principal):
    with pytest.raises(KerberosAuthenticationError, match="could not be verified"):
        _settings().normalize_principal(principal)


def test_kerberos_proxy_deployment_strips_spoofable_headers_and_has_no_host_port():
    root = Path(__file__).parents[1]
    deploy = root / "deploy/kerberos-proxy"
    apache = (deploy / "recordbench-auth.conf.template").read_text(encoding="utf-8")
    compose = (root / "compose.kerberos.yaml").read_text(encoding="utf-8")
    entrypoint = (deploy / "entrypoint").read_text(encoding="utf-8")
    dockerfile = (deploy / "Dockerfile").read_text(encoding="utf-8")

    assert "GssapiAllowedMech krb5" in apache
    assert "GssapiBasicAuth Off" in apache
    assert "GssapiLocalName Off" in apache
    assert "RequestHeader unset X-RecordBench-Authenticated-User early" in apache
    assert "RequestHeader unset X-RecordBench-Proxy-Secret early" in apache
    assert "RequestHeader unset Authorization" in apache
    assert '"expr=%{REMOTE_USER}"' in apache
    assert "recordbench_minimal" in apache
    assert "CustomLog /proc/self/fd/1 combined" not in apache
    assert "read_only: true" in compose
    assert "no-new-privileges:true" in compose
    assert not re.search(r"^\s+ports:\s*$", compose, flags=re.MULTILINE)
    assert "KRB5RCACHEDIR: /run/recordbench-auth/rcache" in compose
    assert "/var/lib/sss/pipes:/var/lib/sss/pipes:ro" in compose
    assert "/etc/krb5.conf:/etc/krb5.conf:ro" in compose
    assert "recordbench.keytab" in entrypoint
    assert "klist -k" in entrypoint
    assert 'install -d -m 0700 -o www-data -g www-data "$runtime/rcache"' in entrypoint
    assert "RECORDBENCH_KERBEROS_PRINCIPAL" in entrypoint
    assert "KRB5RCACHETYPE=none" not in compose + entrypoint
    assert "EXPOSE 8080" in dockerfile
    assert PROXY_SECRET not in apache + compose + entrypoint + dockerfile


@pytest.mark.parametrize("user_mode", ["new-only", "old-only", "both-equal", "both-different"])
@pytest.mark.parametrize("secret_mode", ["new-only", "old-only", "both-equal", "both-different"])
def test_kerberos_header_aliases_at_login_and_session_boundary(tmp_path, user_mode, secret_mode):
    app = create_workbench_app(
        tmp_path / "runtime", generator=UnavailableGenerator(), auth_mode="kerberos",
        secure_cookie=True, kerberos_settings=_settings(), kerberos_profile_resolver=_profile,
        answer_workers=1,
    )
    headers = {}
    for suffix, mode, value in (("Authenticated-User", user_mode, REVIEWER_PRINCIPAL),
                                ("Proxy-Secret", secret_mode, PROXY_SECRET)):
        if mode != "old-only":
            headers[f"X-Exculpata-{suffix}"] = value
        if mode != "new-only":
            headers[f"X-RecordBench-{suffix}"] = "synthetic-conflict" if mode == "both-different" else value
    conflict = "both-different" in (user_mode, secret_mode)
    with TestClient(app, base_url="https://recordbench.example.test") as client:
        result = client.get("/auth/login", headers=headers, follow_redirects=False)
        assert result.status_code == (401 if conflict else 303)
        if conflict:
            assert client.cookies.get(SESSION_COOKIE) is None
            assert app.state.workbench.workspace.connection.execute(
                "SELECT COUNT(*) FROM workbench_principal WHERE provider=?", (_settings().provider_key,)
            ).fetchone()[0] == 0
            assert client.get("/auth/login", headers=_headers(), follow_redirects=False).status_code == 303
        result = client.get("/matters/new", headers=headers, follow_redirects=False)
        assert result.status_code == (401 if conflict else 200)
        # A rejected ambiguous request must not revoke an otherwise valid session.
        assert client.get("/matters/new", headers=_headers()).status_code == 200


@pytest.mark.parametrize("brand", ["Exculpata", "RecordBench"])
@pytest.mark.parametrize("proof", [None, "synthetic-invalid-proof", ""])
def test_each_identity_alias_still_requires_correct_proxy_proof(tmp_path, brand, proof):
    app = create_workbench_app(
        tmp_path / "runtime", generator=UnavailableGenerator(), auth_mode="kerberos",
        secure_cookie=True, kerberos_settings=_settings(), kerberos_profile_resolver=_profile,
    )
    headers = {f"X-{brand}-Authenticated-User": REVIEWER_PRINCIPAL}
    if proof is not None:
        headers[f"X-{brand}-Proxy-Secret"] = proof
    with TestClient(app, base_url="https://recordbench.example.test") as client:
        assert client.get("/auth/login", headers=headers).status_code == 401
        assert client.cookies.get(SESSION_COOKIE) is None
