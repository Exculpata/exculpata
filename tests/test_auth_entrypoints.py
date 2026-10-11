"""Synthetic startup and peer-boundary regressions for explicit authentication."""
from __future__ import annotations

import hashlib
import hmac
import os
from pathlib import Path
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient

from case_intelligence import workbench
from case_intelligence.generation import UnavailableGenerator
from case_intelligence.identity import IdentityService
from case_intelligence.workspace_store import WorkspaceStore


@pytest.mark.parametrize("mode", [None, "", "   ", "invalid"])
def test_factory_refuses_missing_or_invalid_mode_before_runtime_writes(tmp_path, monkeypatch, mode):
    monkeypatch.delenv("CASE_INTELLIGENCE_AUTH_MODE", raising=False)
    if mode is not None:
        monkeypatch.setenv("CASE_INTELLIGENCE_AUTH_MODE", mode)
    runtime = tmp_path / "runtime"
    with pytest.raises(RuntimeError, match="identity provider"):
        workbench.create_workbench_app(runtime, generator=UnavailableGenerator())
    assert not runtime.exists()


def test_explicit_empty_mode_does_not_fall_back_to_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("CASE_INTELLIGENCE_AUTH_MODE", "preview")
    with pytest.raises(RuntimeError, match="identity provider"):
        workbench.create_workbench_app(tmp_path / "runtime", auth_mode="")
    assert not (tmp_path / "runtime").exists()


@pytest.mark.parametrize("mode", [None, "", "preview", "test"])
def test_installed_identity_refuses_bypass_before_seeding(tmp_path, monkeypatch, mode):
    monkeypatch.setenv("RECORDBENCH_ALLOW_CONTAINER_BIND", "1")
    monkeypatch.delenv("CASE_INTELLIGENCE_AUTH_MODE", raising=False)
    store = WorkspaceStore(tmp_path / "workbench.sqlite")
    try:
        with pytest.raises(RuntimeError, match="identity provider|installed"):
            IdentityService(store, tmp_path / "identity", auth_mode=mode)
        assert not (tmp_path / "identity").exists()
        assert not store.connection.execute("SELECT 1 FROM workbench_principal").fetchall()
    finally:
        store.close()


@pytest.mark.parametrize("mode", [None, "", "invalid", "preview", "test"])
def test_real_container_cli_exits_without_creating_runtime(tmp_path, mode):
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith(("CASE_INTELLIGENCE_", "RECORDBENCH_"))}
    environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    environment["RECORDBENCH_ALLOW_CONTAINER_BIND"] = "1"
    if mode is not None:
        environment["CASE_INTELLIGENCE_AUTH_MODE"] = mode
    runtime = tmp_path / "runtime"
    result = subprocess.run(
        [sys.executable, "-m", "case_intelligence.workbench", "--host", "0.0.0.0",
         "--port", "0", "--runtime", str(runtime)],
        env=environment, capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 2
    assert not runtime.exists()
    assert "identity provider" in result.stderr or "installed service" in result.stderr


@pytest.mark.parametrize("host", ["127.0.0.1", "0.0.0.0"])
@pytest.mark.parametrize("mode", [None, "", "invalid", "preview", "test"])
def test_container_cli_rejects_unsafe_modes_before_app_or_listener(tmp_path, monkeypatch, host, mode):
    monkeypatch.setenv("RECORDBENCH_ALLOW_CONTAINER_BIND", "1")
    monkeypatch.delenv("CASE_INTELLIGENCE_AUTH_MODE", raising=False)
    if mode is not None:
        monkeypatch.setenv("CASE_INTELLIGENCE_AUTH_MODE", mode)
    monkeypatch.setattr(sys, "argv", ["recordbench-workbench", "--host", host, "--runtime", str(tmp_path)])
    monkeypatch.setattr(workbench, "create_workbench_app", lambda *a, **k: pytest.fail("created unsafe app"))
    monkeypatch.setattr(workbench.uvicorn, "run", lambda *a, **k: pytest.fail("opened unsafe listener"))
    with pytest.raises(SystemExit) as failure:
        workbench.main()
    assert failure.value.code == 2


@pytest.mark.parametrize("mode", ["local", "oidc", "kerberos"])
def test_container_cli_passes_explicit_provider_and_keeps_peer_addresses(monkeypatch, mode):
    monkeypatch.setenv("RECORDBENCH_ALLOW_CONTAINER_BIND", "1")
    monkeypatch.setenv("CASE_INTELLIGENCE_AUTH_MODE", mode)
    monkeypatch.setattr(sys, "argv", ["recordbench-workbench", "--host", "0.0.0.0"])
    app = object()
    calls = []
    monkeypatch.setattr(workbench, "create_workbench_app", lambda *a, **k: app)
    monkeypatch.setattr(workbench.uvicorn, "run", lambda *a, **k: calls.append((a, k)))
    workbench.main()
    assert calls[0][0] == (app,)
    assert calls[0][1]["proxy_headers"] is False


@pytest.mark.parametrize("mode", ["preview", "test"])
def test_loopback_cli_allows_deliberate_development_mode(monkeypatch, mode):
    monkeypatch.delenv("RECORDBENCH_ALLOW_CONTAINER_BIND", raising=False)
    monkeypatch.setenv("CASE_INTELLIGENCE_AUTH_MODE", mode)
    monkeypatch.setattr(sys, "argv", ["recordbench-workbench"])
    calls = []
    monkeypatch.setattr(workbench, "create_workbench_app", lambda *a, **k: k["auth_mode"])
    monkeypatch.setattr(workbench.uvicorn, "run", lambda *a, **k: calls.append((a, k)))
    workbench.main()
    assert calls == [((mode,), {"host": "127.0.0.1", "port": 8786, "access_log": False, "proxy_headers": False})]


@pytest.mark.parametrize("mode", ["preview", "test"])
def test_explicit_development_mode_denies_remote_reads_identity_and_mutation(tmp_path, monkeypatch, mode):
    monkeypatch.delenv("RECORDBENCH_ALLOW_CONTAINER_BIND", raising=False)
    app = workbench.create_workbench_app(tmp_path / "runtime", auth_mode=mode,
                                       generator=UnavailableGenerator())
    with TestClient(app, client=("192.0.2.44", 4567)) as client:
        for path in ("/", "/auth/login", "/health", "/internal/auth-mode"):
            response = client.get(path, headers={"X-Forwarded-For": "127.0.0.1"})
            assert response.status_code in {403, 404}
            assert "Taylor Morgan" not in response.text
        assert client.post("/auth/login", data={"identity_subject": "taylor-morgan"}).status_code == 403
        assert client.post("/matters", data={"name": "Synthetic remote matter"}).status_code == 403
        assert not app.state.workbench.workspace.connection.execute("SELECT 1 FROM workbench_matter").fetchall()


def test_auth_diagnostic_requires_loopback_and_operator_proof_and_keeps_health_unchanged(tmp_path, monkeypatch):
    monkeypatch.delenv("RECORDBENCH_ALLOW_CONTAINER_BIND", raising=False)
    runtime = tmp_path / "runtime"
    app = workbench.create_workbench_app(runtime, auth_mode="preview", generator=UnavailableGenerator())
    proof = hmac.new((runtime / "identity-session.key").read_bytes(),
                     b"recordbench/auth-mode-diagnostic/v1", hashlib.sha256).hexdigest()
    with TestClient(app, client=("127.0.0.1", 4567)) as client:
        assert client.get("/internal/auth-mode").status_code == 404
        assert client.get("/internal/auth-mode", headers={"X-RecordBench-Auth-Diagnostic": "invalid"}).status_code == 404
        response = client.get("/internal/auth-mode", headers={"X-RecordBench-Auth-Diagnostic": proof})
        assert response.status_code == 200
        assert response.json() == {"auth_mode": "preview"}
        assert response.headers["cache-control"] == "no-store"
        assert "auth_mode" not in client.get("/health").json()
        assert client.get("/auth/login").status_code == 200


def test_remote_diagnostic_denies_even_valid_proof_and_forwarded_loopback(tmp_path, monkeypatch):
    monkeypatch.delenv("RECORDBENCH_ALLOW_CONTAINER_BIND", raising=False)
    runtime = tmp_path / "runtime"
    app = workbench.create_workbench_app(runtime, auth_mode="preview", generator=UnavailableGenerator())
    proof = hmac.new((runtime / "identity-session.key").read_bytes(),
                     b"recordbench/auth-mode-diagnostic/v1", hashlib.sha256).hexdigest()
    with TestClient(app, client=("192.0.2.44", 4567)) as client:
        response = client.get("/internal/auth-mode", headers={
            "X-RecordBench-Auth-Diagnostic": proof, "X-Forwarded-For": "127.0.0.1",
        })
        assert response.status_code == 404
        assert response.content == b""


@pytest.mark.parametrize("mode", ["new-only", "old-only", "both-equal", "both-different"])
def test_diagnostic_aliases_keep_loopback_and_proof_boundary(tmp_path, mode):
    runtime = tmp_path / "runtime"
    app = workbench.create_workbench_app(runtime, auth_mode="preview", generator=UnavailableGenerator())
    proof = hmac.new((runtime / "identity-session.key").read_bytes(),
                     b"recordbench/auth-mode-diagnostic/v1", hashlib.sha256).hexdigest()
    headers = {}
    if mode != "old-only":
        headers["X-Exculpata-Auth-Diagnostic"] = proof
    if mode != "new-only":
        headers["X-RecordBench-Auth-Diagnostic"] = "synthetic-invalid-proof" if mode == "both-different" else proof
    with TestClient(app, client=("127.0.0.1", 4567)) as client:
        response = client.get("/internal/auth-mode", headers=headers)
        assert response.status_code == (404 if mode == "both-different" else 200)
    with TestClient(app, client=("192.0.2.44", 4567)) as client:
        assert client.get("/internal/auth-mode", headers=headers).status_code == 404
