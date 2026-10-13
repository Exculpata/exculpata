from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient
from joserfc import jwt
from joserfc.jwk import RSAKey

from case_intelligence.generation import UnavailableGenerator
from case_intelligence.identity import (
    OIDC_STATE_COOKIE,
    SESSION_COOKIE,
    AuthlibOidcClient,
    OidcAuthenticationError,
    OidcSettings,
)
from case_intelligence.workbench import create_workbench_app


def _settings(
    *,
    allowed_groups: frozenset[str] = frozenset(),
    administrator_groups: frozenset[str] = frozenset(),
) -> OidcSettings:
    return OidcSettings(
        issuer="https://idp.example.test",
        external_origin="https://case.example.test",
        client_id="case-intelligence-test-client",
        client_secret="synthetic-client-secret-value",
        allowed_groups=allowed_groups,
        administrator_groups=administrator_groups,
    )


class FakeOidcProvider:
    def __init__(self, settings: OidcSettings) -> None:
        self.settings = settings
        self.subject = "stable-domain-subject-001"
        self.display_name = "Case Reviewer"
        self.login_name = "case.reviewer@example.test"
        self.groups = ["case-intelligence-users"]
        self.authorization_calls: list[dict[str, str]] = []
        self.authentication_calls: list[dict[str, str]] = []

    async def authorization_url(
        self, *, state: str, nonce: str, code_verifier: str
    ) -> str:
        self.authorization_calls.append(
            {"state": state, "nonce": nonce, "code_verifier": code_verifier}
        )
        challenge = hashlib.sha256(code_verifier.encode("ascii")).digest()
        challenge_value = __import__("base64").urlsafe_b64encode(challenge).rstrip(b"=").decode()
        return "https://idp.example.test/authorize?" + urlencode(
            {
                "response_type": "code",
                "client_id": self.settings.client_id,
                "redirect_uri": self.settings.callback_url,
                "scope": " ".join(self.settings.scopes),
                "state": state,
                "nonce": nonce,
                "code_challenge": challenge_value,
                "code_challenge_method": "S256",
            }
        )

    async def authenticate(
        self, *, code: str, nonce: str, code_verifier: str
    ) -> dict[str, object]:
        self.authentication_calls.append(
            {"code": code, "nonce": nonce, "code_verifier": code_verifier}
        )
        return {
            "iss": self.settings.issuer,
            "sub": self.subject,
            "aud": self.settings.client_id,
            "name": self.display_name,
            "preferred_username": self.login_name,
            "groups": list(self.groups),
        }


def _begin(client: TestClient, next_path: str = "/matters/new") -> str:
    response = client.get(
        "/auth/oidc/start",
        params={"next": next_path},
        follow_redirects=False,
    )
    assert response.status_code == 302
    assert response.headers["location"].startswith("https://idp.example.test/authorize?")
    state = client.cookies.get(OIDC_STATE_COOKIE)
    assert state
    return state


def test_oidc_route_creates_one_time_server_session_and_stable_principal(tmp_path):
    settings = _settings(allowed_groups=frozenset({"case-intelligence-users"}))
    provider = FakeOidcProvider(settings)
    app = create_workbench_app(
        tmp_path / "runtime",
        generator=UnavailableGenerator(),
        auth_mode="oidc",
        secure_cookie=True,
        oidc_settings=settings,
        oidc_client=provider,
        answer_workers=1,
    )
    with TestClient(app, base_url=settings.external_origin) as client:
        page = client.get("/auth/login", params={"next": "/matters/new"})
        assert page.status_code == 200
        assert "Sign in with your organization account" in page.text
        assert "Taylor Morgan" not in page.text

        state = _begin(client)
        started = provider.authorization_calls[-1]
        assert len(started["code_verifier"]) == 43
        assert started["code_verifier"] != state
        columns = {
            row[1]
            for row in app.state.workbench.workspace.connection.execute(
                "PRAGMA table_info(workbench_oidc_transaction)"
            )
        }
        assert columns == {
            "state_digest",
            "next_path",
            "created_at",
            "expires_at",
            "consumed_at",
        }
        database_bytes = (tmp_path / "runtime/workbench.sqlite").read_bytes()
        assert started["code_verifier"].encode() not in database_bytes
        assert started["nonce"].encode() not in database_bytes
        assert settings.client_secret.encode() not in database_bytes

        callback = client.get(
            "/auth/oidc/callback",
            params={"code": "synthetic-authorization-code", "state": state},
            follow_redirects=False,
        )
        assert callback.status_code == 303
        assert callback.headers["location"] == "/matters/new"
        session_token = client.cookies.get(SESSION_COOKIE)
        assert session_token
        landing = client.get("/matters/new")
        assert landing.status_code == 200
        assert "Case Reviewer" in landing.text
        assert "signed in with your organization account as Case Reviewer" in landing.text
        assert "Organization account sign-in is not connected yet" not in landing.text

        principal = app.state.workbench.workspace.connection.execute(
            "SELECT principal_id,provider,provider_subject FROM workbench_principal "
            "WHERE provider=? AND provider_subject=?",
            (settings.provider_key, provider.subject),
        ).fetchone()
        assert principal is not None
        original_principal_id = principal["principal_id"]

        client.cookies.delete(SESSION_COOKIE, path="/")
        client.cookies.set(
            OIDC_STATE_COOKIE,
            state,
            domain="case.example.test",
            path="/auth/oidc",
        )
        replay = client.get(
            "/auth/oidc/callback",
            params={"code": "replayed-code", "state": state},
            follow_redirects=False,
        )
        assert replay.status_code == 303
        assert replay.headers["location"].startswith("/auth/login?error=")
        assert len(provider.authentication_calls) == 1

        provider.display_name = "Case Reviewer-Smith"
        provider.login_name = "case.reviewer-smith@example.test"
        second_state = _begin(client, "/matters/new")
        second = client.get(
            "/auth/oidc/callback",
            params={"code": "second-authorization-code", "state": second_state},
            follow_redirects=False,
        )
        assert second.status_code == 303
        renamed = app.state.workbench.workspace.connection.execute(
            "SELECT principal_id,display_name,login_name FROM workbench_principal "
            "WHERE provider=? AND provider_subject=?",
            (settings.provider_key, provider.subject),
        ).fetchone()
        assert renamed["principal_id"] == original_principal_id
        assert renamed["display_name"] == "Case Reviewer-Smith"
        assert renamed["login_name"] == "case.reviewer-smith@example.test"

        events = app.state.workbench.workspace.connection.execute(
            "SELECT outcome,details_json FROM workbench_audit_event "
            "WHERE action='auth.login' ORDER BY occurred_at,event_id"
        ).fetchall()
        assert {row["outcome"] for row in events} >= {"success", "denied"}
        serialized = json.dumps([dict(row) for row in events])
        assert "synthetic-authorization-code" not in serialized
        assert settings.client_secret not in serialized


def test_oidc_group_admission_is_not_matter_membership(tmp_path):
    settings = _settings(allowed_groups=frozenset({"approved-group"}))
    provider = FakeOidcProvider(settings)
    provider.groups = ["different-group"]
    app = create_workbench_app(
        tmp_path / "runtime",
        generator=UnavailableGenerator(),
        auth_mode="oidc",
        secure_cookie=True,
        oidc_settings=settings,
        oidc_client=provider,
        answer_workers=1,
    )
    with TestClient(app, base_url=settings.external_origin) as client:
        state = _begin(client)
        denied = client.get(
            "/auth/oidc/callback",
            params={"code": "authorization-code", "state": state},
            follow_redirects=False,
        )
        assert denied.status_code == 303
        assert client.cookies.get(SESSION_COOKIE) is None
        count = app.state.workbench.workspace.connection.execute(
            "SELECT COUNT(*) FROM workbench_principal WHERE provider=?",
            (settings.provider_key,),
        ).fetchone()[0]
        assert count == 0


def test_oidc_administrator_group_is_persisted_for_the_server_session(tmp_path):
    settings = _settings(
        allowed_groups=frozenset({"review-users"}),
        administrator_groups=frozenset({"review-administrators"}),
    )
    provider = FakeOidcProvider(settings)
    provider.groups = ["review-users", "review-administrators"]
    app = create_workbench_app(
        tmp_path / "runtime",
        generator=UnavailableGenerator(),
        auth_mode="oidc",
        secure_cookie=True,
        oidc_settings=settings,
        oidc_client=provider,
        answer_workers=1,
    )
    with TestClient(app, base_url=settings.external_origin) as client:
        state = _begin(client, "/admin")
        callback = client.get(
            "/auth/oidc/callback",
            params={"code": "administrator-code", "state": state},
            follow_redirects=False,
        )
        assert callback.status_code == 303
        assert callback.headers["location"] == "/admin"
        admin = client.get("/admin")
        assert admin.status_code == 200
        assert "Administration" in admin.text
        row = app.state.workbench.workspace.connection.execute(
            "SELECT application_roles FROM workbench_session WHERE revoked_at IS NULL"
        ).fetchone()
        assert json.loads(row["application_roles"]) == ["administrator"]


def test_oidc_environment_requires_secure_owner_only_secret_file(tmp_path, monkeypatch):
    secret_file = tmp_path / "oidc-client-secret"
    secret_file.write_text("synthetic-client-secret-value\n", encoding="utf-8")
    secret_file.chmod(0o644)
    monkeypatch.setenv("CASE_INTELLIGENCE_OIDC_ISSUER", "https://idp.example.test")
    monkeypatch.setenv("CASE_INTELLIGENCE_EXTERNAL_ORIGIN", "https://case.example.test")
    monkeypatch.setenv("CASE_INTELLIGENCE_OIDC_CLIENT_ID", "test-client")
    monkeypatch.setenv("CASE_INTELLIGENCE_OIDC_CLIENT_SECRET_FILE", str(secret_file))
    monkeypatch.setenv("CASE_INTELLIGENCE_SECURE_COOKIE", "1")

    with pytest.raises(RuntimeError, match="mode 0600"):
        create_workbench_app(
            tmp_path / "unsafe",
            generator=UnavailableGenerator(),
            auth_mode="oidc",
            answer_workers=1,
        )

    secret_file.chmod(0o600)
    app = create_workbench_app(
        tmp_path / "valid",
        generator=UnavailableGenerator(),
        auth_mode="oidc",
        answer_workers=1,
    )
    assert app.state.identity.oidc_settings.client_secret == "synthetic-client-secret-value"
    assert "synthetic-client-secret-value" not in repr(app.state.identity.oidc_settings)
    app.state.workbench.close()

    monkeypatch.setenv("CASE_INTELLIGENCE_SECURE_COOKIE", "0")
    with pytest.raises(RuntimeError, match="secure cookie"):
        create_workbench_app(
            tmp_path / "insecure-cookie",
            generator=UnavailableGenerator(),
            auth_mode="oidc",
            answer_workers=1,
        )


@pytest.mark.parametrize(
    "failure",
    [None, "audience", "signature", "issuer", "subject", "nonce", "expired"],
)
@pytest.mark.parametrize("max_age", [None, 0])
def test_authlib_adapter_validates_signed_id_token_and_rejects_tampering(failure, max_age):
    settings = _settings()
    approved_key = RSAKey.generate_key(2048, parameters={"kid": "approved-key"})
    unapproved_key = RSAKey.generate_key(2048, parameters={"kid": "approved-key"})
    expected_nonce = "synthetic-nonce"
    expected_verifier = "v" * 43

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/.well-known/openid-configuration":
            return httpx.Response(
                200,
                json={
                    "issuer": settings.issuer,
                    "authorization_endpoint": settings.issuer + "/authorize",
                    "token_endpoint": settings.issuer + "/token",
                    "jwks_uri": settings.issuer + "/jwks",
                    "response_types_supported": ["code"],
                    "code_challenge_methods_supported": ["S256"],
                    "token_endpoint_auth_methods_supported": ["client_secret_post"],
                    "id_token_signing_alg_values_supported": ["RS256", "none"],
                },
            )
        if path == "/jwks":
            return httpx.Response(
                200,
                json={"keys": [approved_key.as_dict(private=False)]},
            )
        if path == "/token":
            form = parse_qs(request.content.decode("utf-8"))
            assert form["code_verifier"] == [expected_verifier]
            assert form["client_secret"] == [settings.client_secret]
            now = int(time.time())
            claims = {
                "iss": "https://wrong.example.test" if failure == "issuer" else settings.issuer,
                "aud": (
                    "wrong-client" if failure == "audience" else settings.client_id
                ),
                "exp": now - 120 if failure == "expired" else now + 300,
                "iat": now,
                "nonce": "wrong-nonce" if failure == "nonce" else expected_nonce,
                "name": "Signed Reviewer",
                "preferred_username": "signed.reviewer@example.test",
            }
            if failure != "subject":
                claims["sub"] = "signed-subject"
            signing_key = unapproved_key if failure == "signature" else approved_key
            encoded = jwt.encode(
                {"alg": "RS256", "kid": "approved-key"},
                claims,
                signing_key,
            )
            return httpx.Response(
                200,
                json={
                    "access_token": "synthetic-access-token",
                    "token_type": "Bearer",
                    "expires_in": 300,
                    "id_token": encoded,
                },
            )
        return httpx.Response(404)

    async def exercise() -> None:
        client = AuthlibOidcClient(settings)
        client.remote.client_kwargs["transport"] = httpx.MockTransport(handler)
        url = await client.authorization_url(
            state="synthetic-state",
            nonce=expected_nonce,
            code_verifier=expected_verifier, max_age=max_age,
        )
        query = parse_qs(urlsplit(url).query)
        assert query["code_challenge_method"] == ["S256"]
        assert query["state"] == ["synthetic-state"]
        assert query.get("max_age") == (["0"] if max_age == 0 else None)
        assert query["scope"] == [" ".join(settings.scopes)]
        if failure is None:
            claims = await client.authenticate(
                code="synthetic-code",
                nonce=expected_nonce,
                code_verifier=expected_verifier,
            )
            assert claims["sub"] == "signed-subject"
        else:
            with pytest.raises(OidcAuthenticationError, match="could not be verified"):
                await client.authenticate(
                    code="synthetic-code",
                    nonce=expected_nonce,
                    code_verifier=expected_verifier,
                )

    asyncio.run(exercise())
