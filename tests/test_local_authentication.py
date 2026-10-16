from __future__ import annotations


from case_intelligence.legacy_names import (
    LEGACY_STEM,
)
import json
import re

import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient

from case_intelligence.generation import UnavailableGenerator
from case_intelligence.identity import LocalAccountSettings, SESSION_COOKIE
from case_intelligence.local_accounts import LocalAccountRepository
from case_intelligence.workbench import create_workbench_app


def _accounts_file(tmp_path):
    path = tmp_path / "accounts.json"
    path.write_text(
        json.dumps(
            {
                "format_version": 1,
                "accounts": [
                    {
                        "username": "alice.reviewer",
                        "display_name": "Alice Reviewer",
                        "password_hash": PasswordHasher().hash(
                            "synthetic-local-password"
                        ),
                        "roles": ["administrator"],
                        "enabled": True,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    path.chmod(0o600)
    return path


def _challenge(page: str) -> str:
    match = re.search(r'name="login_challenge" value="([^"]+)"', page)
    assert match is not None
    return match.group(1)


def test_local_account_login_uses_argon2_session_and_admin_role(tmp_path):
    settings = LocalAccountSettings(_accounts_file(tmp_path))
    app = create_workbench_app(
        tmp_path / "runtime",
        generator=UnavailableGenerator(),
        auth_mode="local",
        secure_cookie=True,
        local_settings=settings,
        answer_workers=1,
    )
    with TestClient(app, base_url=f"https://{LEGACY_STEM}.example.test") as client:
        page = client.get("/auth/login")
        assert page.status_code == 200
        assert "Alice Reviewer" not in page.text
        assert "Argon2id" in page.text
        challenge = _challenge(page.text)

        denied = client.post(
            "/auth/local",
            data={
                "username": "alice.reviewer",
                "password": "incorrect-password",
                "login_challenge": challenge,
                "next": "/matters/new",
            },
            follow_redirects=False,
        )
        assert denied.status_code == 303
        assert "username+or+password" in denied.headers["location"]
        assert client.cookies.get(SESSION_COOKIE) is None

        refreshed = client.get("/auth/login")
        login = client.post(
            "/auth/local",
            data={
                "username": "ALICE.REVIEWER",
                "password": "synthetic-local-password",
                "login_challenge": _challenge(refreshed.text),
                "next": "/matters/new",
            },
            follow_redirects=False,
        )
        assert login.status_code == 303
        assert login.headers["location"] == "/matters/new"
        assert "Secure" in login.headers["set-cookie"]
        assert "HttpOnly" in login.headers["set-cookie"]

        landing = client.get("/matters/new")
        assert landing.status_code == 200
        assert "Alice Reviewer" in landing.text
        assert "Administrator" in landing.text
        assert "/admin" in landing.text
        assert "Local account" in landing.text
        assert "signed in with a private installation account" in landing.text
        assert "local preview" not in landing.text.casefold()


def test_local_mode_requires_secure_cookie_and_private_account_file(tmp_path):
    accounts = _accounts_file(tmp_path)
    settings = LocalAccountSettings(accounts)
    with pytest.raises(RuntimeError, match="secure cookies"):
        create_workbench_app(
            tmp_path / "runtime-insecure",
            generator=UnavailableGenerator(),
            auth_mode="local",
            secure_cookie=False,
            local_settings=settings,
            answer_workers=1,
        )

    accounts.chmod(0o644)
    with pytest.raises(RuntimeError, match="mode 0600"):
        LocalAccountSettings(accounts)


def test_disabled_local_account_cannot_reuse_a_prior_session(tmp_path):
    accounts = _accounts_file(tmp_path)
    settings = LocalAccountSettings(accounts)
    app = create_workbench_app(
        tmp_path / "runtime",
        generator=UnavailableGenerator(),
        auth_mode="local",
        secure_cookie=True,
        local_settings=settings,
        answer_workers=1,
    )
    with TestClient(app, base_url=f"https://{LEGACY_STEM}.example.test") as client:
        page = client.get("/auth/login")
        login = client.post(
            "/auth/local",
            data={
                "username": "alice.reviewer",
                "password": "synthetic-local-password",
                "login_challenge": _challenge(page.text),
                "next": "/",
            },
            follow_redirects=False,
        )
        assert login.status_code == 303
        token = client.cookies.get(SESSION_COOKIE)
        assert token
        repository = LocalAccountRepository(accounts)
        repository.migrate(tmp_path / "recovery/accounts-v1.json", actor="synthetic-operator")
        repository.create("backup.admin", "Backup Administrator", "synthetic-backup-password",
                          administrator=True, actor="synthetic-operator")
        repository.set_enabled("alice.reviewer", False, actor="synthetic-operator")
        blocked = client.get("/", follow_redirects=False)
        assert blocked.status_code == 303
        assert blocked.headers["location"].startswith("/auth/login")
