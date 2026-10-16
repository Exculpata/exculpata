from __future__ import annotations

import json
import re

from argon2 import PasswordHasher
from fastapi.testclient import TestClient

from case_intelligence.generation import UnavailableGenerator
from case_intelligence.identity import LocalAccountSettings
from case_intelligence.managed_storage import node_storage_profile
from case_intelligence.workbench import create_workbench_app

BANNER = "Evaluation node, not for real case data"


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
                        "password_hash": PasswordHasher().hash("synthetic-local-password"),
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


def _app(tmp_path):
    return create_workbench_app(
        tmp_path / "runtime",
        generator=UnavailableGenerator(),
        auth_mode="local",
        secure_cookie=True,
        local_settings=LocalAccountSettings(_accounts_file(tmp_path)),
        answer_workers=1,
    )


def test_storage_profile_defaults_to_production(monkeypatch):
    monkeypatch.delenv("CASE_INTELLIGENCE_STORAGE_PROFILE", raising=False)
    assert node_storage_profile() == "production"
    monkeypatch.setenv("CASE_INTELLIGENCE_STORAGE_PROFILE", "production")
    assert node_storage_profile() == "production"
    monkeypatch.setenv("CASE_INTELLIGENCE_STORAGE_PROFILE", "evaluation")
    assert node_storage_profile() == "evaluation"


def test_production_node_has_no_evaluation_banner(tmp_path, monkeypatch):
    monkeypatch.delenv("CASE_INTELLIGENCE_STORAGE_PROFILE", raising=False)
    monkeypatch.setenv("CASE_INTELLIGENCE_STORAGE_RESERVE_GIB", "10")
    app = _app(tmp_path)
    with TestClient(app, base_url="https://recordbench.example.test") as client:
        login = client.get("/auth/login")
        assert login.status_code == 200
        assert BANNER not in login.text
        signed_in = client.post(
            "/auth/local",
            data={
                "username": "alice.reviewer",
                "password": "synthetic-local-password",
                "login_challenge": _challenge(login.text),
                "next": "/matters/new",
            },
            follow_redirects=False,
        )
        assert signed_in.status_code == 303
        matter = client.get("/matters/new")
        assert matter.status_code == 200
        assert BANNER not in matter.text


def test_evaluation_banner_persists_across_login_and_matter_pages(tmp_path, monkeypatch):
    monkeypatch.setenv("CASE_INTELLIGENCE_STORAGE_PROFILE", "evaluation")
    monkeypatch.setenv("CASE_INTELLIGENCE_STORAGE_RESERVE_GIB", "0")
    app = _app(tmp_path)
    with TestClient(app, base_url="https://recordbench.example.test") as client:
        login = client.get("/auth/login")
        assert login.status_code == 200
        assert login.text.count(BANNER) == 1
        assert 'class="evaluation-node-banner"' in login.text
        signed_in = client.post(
            "/auth/local",
            data={
                "username": "alice.reviewer",
                "password": "synthetic-local-password",
                "login_challenge": _challenge(login.text),
                "next": "/matters/new",
            },
            follow_redirects=False,
        )
        assert signed_in.status_code == 303
        matter = client.get("/matters/new")
        admin = client.get("/admin")
        assert matter.status_code == 200 and admin.status_code == 200
        assert matter.text.count(BANNER) == 1
        assert admin.text.count(BANNER) == 1
        assert 'role="status"' in matter.text
