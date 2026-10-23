"""Synthetic request-boundary checks for directory sign-in header pinning."""

import pytest
from fastapi.testclient import TestClient
from starlette.datastructures import Headers

from case_intelligence import compat_env
from case_intelligence.generation import UnavailableGenerator
from case_intelligence.header_policy import header_family_allowed
from case_intelligence.identity import KerberosSettings, SESSION_COOKIE
from case_intelligence.legacy_names import (
    CURRENT_ENV_PREFIX, CURRENT_HEADER_PREFIX, LEGACY_ENV_PREFIX, LEGACY_HEADER_PREFIX,
)
from case_intelligence.workbench import create_workbench_app


SETTING = "CASE_INTELLIGENCE_KERBEROS_HEADER_FAMILY"
PRINCIPAL = "synthetic.reviewer@EXAMPLE.TEST"
PROOF = "synthetic_proxy_proof_0000000000000000"
PREFIXES = {"current": CURRENT_HEADER_PREFIX, "legacy": LEGACY_HEADER_PREFIX}


@pytest.fixture
def directory_environment(tmp_path, monkeypatch):
    proof_file = tmp_path / "proxy-proof"
    # fixture-write: Synthetic proxy proof exercises the real settings loader.
    proof_file.write_text(PROOF, encoding="ascii")
    proof_file.chmod(0o600)
    monkeypatch.setenv("CASE_INTELLIGENCE_KERBEROS_PROXY_SECRET_FILE", str(proof_file))
    monkeypatch.setenv("CASE_INTELLIGENCE_KERBEROS_REALM", "EXAMPLE.TEST")
    monkeypatch.setenv("CASE_INTELLIGENCE_KERBEROS_ALLOWED_PRINCIPALS", PRINCIPAL)
    monkeypatch.delenv(SETTING, raising=False)


def _headers(family):
    prefix = PREFIXES[family]
    return {f"{prefix}Authenticated-User": PRINCIPAL, f"{prefix}Proxy-Secret": PROOF}


@pytest.mark.parametrize("family", ["current", "legacy"])
def test_raw_mixed_case_header_is_classified_by_family(family):
    other = "legacy" if family == "current" else "current"
    accepted_name = f"{PREFIXES[family]}Authenticated-User".lower()
    rejected_name = f"{PREFIXES[other]}Authenticated-User"
    headers = Headers(raw=[
        (accepted_name.encode("ascii"), PRINCIPAL.encode("ascii")),
        (rejected_name.encode("ascii"), PRINCIPAL.encode("ascii")),
    ])
    assert rejected_name in list(headers)
    assert compat_env.header(headers, f"{LEGACY_HEADER_PREFIX}Authenticated-User") == PRINCIPAL
    assert header_family_allowed(headers, "both") is True
    assert header_family_allowed(headers, family) is False


def test_unknown_family_is_rejected_for_a_valid_header():
    headers = Headers(_headers("current"))
    assert compat_env.header(headers, f"{LEGACY_HEADER_PREFIX}Authenticated-User") == PRINCIPAL
    assert header_family_allowed(headers, "current") is True
    assert header_family_allowed(headers, "unknown") is False


@pytest.mark.parametrize("prefix", [CURRENT_ENV_PREFIX, LEGACY_ENV_PREFIX])
def test_header_family_environment_alias_is_ignored(monkeypatch, directory_environment, prefix):
    for alias_prefix in (CURRENT_ENV_PREFIX, LEGACY_ENV_PREFIX):
        monkeypatch.delenv(f"{alias_prefix}KERBEROS_HEADER_FAMILY", raising=False)
    monkeypatch.setenv(f"{prefix}KERBEROS_HEADER_FAMILY", "current")
    assert KerberosSettings.from_env().header_family == "both"


def _app(tmp_path):
    return create_workbench_app(
        tmp_path / "runtime", generator=UnavailableGenerator(),
        auth_mode="kerberos", secure_cookie=True,
        kerberos_profile_resolver=lambda principal: ("Synthetic Reviewer", principal),
        answer_workers=1,
    )


@pytest.mark.parametrize("family", ["current", "legacy"])
def test_pinned_family_rejects_other_headers_before_session_processing(
    tmp_path, monkeypatch, directory_environment, family,
):
    monkeypatch.setenv(SETTING, family)
    app = _app(tmp_path)
    accepted = _headers(family)
    other = "legacy" if family == "current" else "current"
    with TestClient(app, base_url="https://app.example.test") as client:
        login = client.get("/auth/login", headers=accepted, follow_redirects=False)
        assert login.status_code == 303
        token = client.cookies.get(SESSION_COOKIE)
        assert token
        assert client.get("/matters/new", headers=accepted).status_code == 200
        resolve = app.state.identity.resolve
        resolved_tokens = []

        def track_resolve(raw_token):
            resolved_tokens.append(raw_token)
            return resolve(raw_token)

        with monkeypatch.context() as boundary:
            boundary.setattr(app.state.identity, "resolve", track_resolve)
            for path in ("/auth/login", "/matters/new", "/health", "/internal/auth-mode"):
                for suffix, value in (
                    ("Authenticated-User", PRINCIPAL), ("Proxy-Secret", PROOF),
                    ("Authenticated-User", ""), ("Proxy-Secret", ""),
                    ("Auth-Diagnostic", ""), ("Unknown", "synthetic"),
                ):
                    headers = {**accepted, f"{PREFIXES[other]}{suffix}".swapcase(): value}
                    response = client.get(path, headers=headers, follow_redirects=False)
                    assert response.status_code == 401
                    assert response.headers["cache-control"] == "no-store"
                    assert "set-cookie" not in response.headers
                response = client.get(path, headers=_headers(other), follow_redirects=False)
                assert response.status_code == 401
            assert resolved_tokens == []
        assert resolve(token) is not None
        assert client.get("/matters/new", headers=accepted).status_code == 200


@pytest.mark.parametrize("family", ["current", "legacy"])
def test_wrong_family_cannot_provision_a_principal(
    tmp_path, monkeypatch, directory_environment, family,
):
    monkeypatch.setenv(SETTING, family)
    app = _app(tmp_path)
    other = "legacy" if family == "current" else "current"
    with TestClient(app, base_url="https://app.example.test") as client:
        response = client.get("/auth/login", headers=_headers(other), follow_redirects=False)
        assert response.status_code == 401
        assert client.cookies.get(SESSION_COOKIE) is None
        assert app.state.workbench.workspace.connection.execute(
            "SELECT COUNT(*) FROM workbench_principal"
        ).fetchone()[0] == 0


@pytest.mark.parametrize("family", [None, "both"])
def test_compatible_families_keep_equal_copies_and_reject_conflicts(
    tmp_path, monkeypatch, directory_environment, family,
):
    if family is not None:
        monkeypatch.setenv(SETTING, family)
    app = _app(tmp_path)
    with TestClient(app, base_url="https://app.example.test") as client:
        headers = {**_headers("current"), **_headers("legacy")}
        assert client.get("/auth/login", headers=headers, follow_redirects=False).status_code == 303
        assert client.get("/matters/new", headers=headers).status_code == 200
        for suffix in ("Authenticated-User", "Proxy-Secret"):
            conflict = {**headers, f"{LEGACY_HEADER_PREFIX}{suffix}": "synthetic-conflict"}
            assert client.get("/matters/new", headers=conflict).status_code == 401


@pytest.mark.parametrize("family", ["", "CURRENT", "unknown", " current "])
def test_invalid_family_fails_at_settings_load(monkeypatch, directory_environment, family):
    monkeypatch.setenv(SETTING, family)
    with pytest.raises(RuntimeError, match="Directory sign-in header family is invalid"):
        KerberosSettings.from_env()
