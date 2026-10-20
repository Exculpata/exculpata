"""Synthetic regressions for local password lanes and device lifecycle."""

import hashlib
import hmac
import json
import re
import secrets
import shutil
import socket
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from http.cookies import SimpleCookie
from pathlib import Path
from unittest.mock import Mock
from urllib.parse import unquote_plus

import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient
from starlette.requests import Request
from starlette.responses import Response

from case_intelligence import workbench
from case_intelligence.generation import UnavailableGenerator
from case_intelligence.identity import (
    IdentityService, LocalAccountSettings, LocalAuthenticationError,
    LOGIN_CHALLENGE_COOKIE, SESSION_COOKIE,
)
from case_intelligence.local_account_format import LocalAccount
from case_intelligence.local_login_devices import (
    DEVICE_COOKIE, DEVICE_MAX_AGE, LocalLoginDevices, set_device_cookie,
)
from case_intelligence.local_login_limiter import LocalLoginLimiter, LoginThrottled, normalize_client_key
from case_intelligence.workbench import create_workbench_app
from case_intelligence.workspace_store import WorkspaceStore

GATEWAY_PEER = ("192.0.2.10", 41000)
PASSWORD = "synthetic-correct-password"


def _accounts(tmp_path):
    path = tmp_path / "accounts.json"
    path.write_text(json.dumps({"format_version": 1, "accounts": [{
        "username": "synthetic.admin", "display_name": "Synthetic Admin",
        "password_hash": PasswordHasher().hash(PASSWORD),
        "roles": ["administrator"], "enabled": True}]}), encoding="utf-8")
    path.chmod(0o600)
    return path


def _login(client, password, address):
    page = client.get("/auth/login")
    challenge = re.search(r'name="login_challenge" value="([^"]+)"', page.text)
    assert challenge is not None
    return client.post("/auth/local", headers={"X-Forwarded-For": address}, data={
        "username": "synthetic.admin", "password": password,
        "login_challenge": challenge.group(1), "next": "/"}, follow_redirects=False)


def _assert_denied(response, client):
    assert response.status_code == 303
    assert response.headers["location"].startswith("/auth/login?")
    assert client.cookies.get(SESSION_COOKIE) is None


def test_known_device_signs_in_while_untrusted_lane_locked(tmp_path):
    app = create_workbench_app(
        tmp_path / "runtime", generator=UnavailableGenerator(), auth_mode="local",
        secure_cookie=True, local_settings=LocalAccountSettings(_accounts(tmp_path)),
        local_gateway_peers=(GATEWAY_PEER[0],), answer_workers=1)
    base = "https://exculpata.example.test"
    with ExitStack() as clients:
        victim = clients.enter_context(TestClient(app, base_url=base, client=GATEWAY_PEER))
        response = _login(victim, PASSWORD, "198.51.100.7")
        assert response.status_code == 303
        destination = response.headers["location"]
        assert not destination.startswith("/auth/login")
        assert victim.cookies.get(SESSION_COOKIE) is not None
        del victim.cookies[SESSION_COOKIE]

        for number in range(1, 11):
            attacker = clients.enter_context(
                TestClient(app, base_url=base, client=GATEWAY_PEER))
            for _ in range(10):
                _assert_denied(
                    _login(attacker, "synthetic-wrong-password", f"203.0.113.{number}"),
                    attacker)

        fresh = clients.enter_context(TestClient(app, base_url=base, client=GATEWAY_PEER))
        _assert_denied(_login(fresh, PASSWORD, "198.51.100.99"), fresh)

        response = _login(victim, PASSWORD, "198.51.100.7")
        assert response.status_code == 303
        assert response.headers["location"] == destination
        assert victim.cookies.get(SESSION_COOKIE) is not None



USER = "synthetic.admin"
OTHER = "synthetic.other"
ADDRESS = "198.51.100.7"


class Clock:
    def __init__(self):
        self.now = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def __call__(self):
        return self.now

    def advance(self, **kwargs):
        self.now += timedelta(**kwargs)


def _settings():
    accounts = {name: LocalAccount(name, "Synthetic Account", "synthetic-hash",
                                  frozenset({"administrator"}), session_revision=secrets.token_hex(32))
                for name in (USER, OTHER)}
    settings = Mock(spec=LocalAccountSettings, provider_key="local")
    settings.account.side_effect = lambda username: accounts.get(username.strip().casefold())
    settings.authenticate.side_effect = lambda username, password: (
        accounts.get(username.strip().casefold()) if password == PASSWORD else None)
    return settings, accounts


@pytest.fixture
def lane_identity(tmp_path):
    settings, accounts = _settings()
    store = WorkspaceStore(tmp_path / "runtime/workbench.sqlite")
    clock = Clock()
    identity = IdentityService(store, tmp_path / "runtime", auth_mode="local", secure_cookie=True,
                               local_settings=settings, clock=clock)
    try:
        yield identity, clock, accounts
    finally:
        store.close()


def _sign_in(identity, *, cookie=None, password=PASSWORD, username=USER, address=ADDRESS):
    return identity.login_local(username, password, client_key=address, device_cookie=cookie)


def _failure(identity, **kwargs):
    with pytest.raises(LocalAuthenticationError) as error:
        _sign_in(identity, password="synthetic-wrong-password", **kwargs)
    return error.value


def _lock_untrusted(identity, username=USER):
    for number in range(1, 11):
        for _ in range(10):
            error = _failure(identity, username=username, address=f"203.0.113.{number}")
            assert error.retry_after_seconds is None


def _rows(identity):
    with sqlite3.connect(identity._local_devices.path) as db:
        db.row_factory = sqlite3.Row
        return [dict(row) for row in db.execute("SELECT * FROM devices ORDER BY rowid")]


@pytest.fixture
def login_handler(tmp_path):
    settings, _ = _settings()
    app = create_workbench_app(
        tmp_path / "runtime", generator=UnavailableGenerator(), auth_mode="local",
        secure_cookie=True, local_settings=settings, local_gateway_peers=(GATEWAY_PEER[0],),
        answer_workers=1)
    identity = app.state.identity
    identity._clock = Clock()
    endpoint = next(route.endpoint for route in app.routes if getattr(route, "path", None) == "/auth/local")

    def login(cookie=None, password=PASSWORD, address=ADDRESS):
        challenge = identity.new_login_challenge()
        cookies = f"{LOGIN_CHALLENGE_COOKIE}={challenge}"
        if cookie:
            cookies += f"; {DEVICE_COOKIE}={cookie}"
        request = Request({"type": "http", "client": GATEWAY_PEER, "headers": [
            (b"cookie", cookies.encode()), (b"x-forwarded-for", address.encode())]})
        return endpoint(request, username=USER, password=password, login_challenge=challenge, next_path="/")

    try:
        yield identity, login
    finally:
        app.state.workbench.close()


def _response_device(response):
    cookies = SimpleCookie()
    for header in response.headers.getlist("set-cookie"):
        cookies.load(header)
    return cookies[DEVICE_COOKIE].value


def test_new_browser_still_refused_while_untrusted_lane_locked(login_handler):
    identity, login = login_handler
    _lock_untrusted(identity)
    response = login()
    assert response.status_code == 303
    assert int(response.headers["retry-after"]) == 900
    assert "try again after 00:15 utc" in unquote_plus(response.headers["location"]).lower()
    assert SESSION_COOKIE not in response.headers.get("set-cookie", "")


def test_unit_known_device_signs_in_while_untrusted_lane_locked(login_handler):
    identity, login = login_handler
    cookie = _response_device(login())
    _lock_untrusted(identity)
    assert login().headers["location"].startswith("/auth/login?")
    response = login(cookie)
    assert response.status_code == 303
    assert not response.headers["location"].startswith("/auth/login?")
    assert _response_device(response) != cookie


def test_device_cookie_rotates_on_success_and_old_value_is_rejected(lane_identity):
    identity, _, _ = lane_identity
    old = _sign_in(identity).device_cookie
    new = _sign_in(identity, cookie=old).device_cookie
    assert old != new
    rows = _rows(identity)
    assert [row["state"] for row in rows] == ["rotated", "active"]
    assert rows[0]["chain_id"] == rows[1]["chain_id"]
    assert old.split(".")[1] not in repr(rows)
    _lock_untrusted(identity)
    with pytest.raises(LocalAuthenticationError) as error:
        _sign_in(identity, cookie=old)
    assert error.value.retry_after_seconds == 900
    assert error.value.device_replayed
    with pytest.raises(LocalAuthenticationError):
        _sign_in(identity, cookie=new)


@pytest.mark.parametrize("locked", [False, True])
def test_replayed_rotated_device_cookie_revokes_chain_and_audits(login_handler, locked):
    identity, login = login_handler
    old = _response_device(login())
    current = _response_device(login(old))
    chain = _rows(identity)[0]["chain_id"]
    if locked:
        _lock_untrusted(identity)
    response = login(old)
    assert response.headers["location"].startswith("/auth/login?") == locked
    assert all(row["state"] == "revoked" for row in _rows(identity) if row["chain_id"] == chain)
    events = [event for event in identity.store.audit_events() if event.action == "auth.device.replay"]
    assert len(events) == 1 and events[0].outcome == "denied"
    serialized = json.dumps(asdict(events[0]))
    assert all(value not in serialized for value in (old, current, ADDRESS, GATEWAY_PEER[0], USER))
    assert events[0].details == {"auth_method": "local"}


@pytest.mark.parametrize("change", ["password", "revision", "role", "enabled"])
def test_device_cookie_bound_to_username_and_session_revision(lane_identity, change):
    identity, _, accounts = lane_identity
    cookie = _sign_in(identity).device_cookie
    _lock_untrusted(identity, OTHER)
    with pytest.raises(LocalAuthenticationError):
        _sign_in(identity, cookie=cookie, username=OTHER)
    changes = {"password": {"password_hash": "synthetic-replacement-hash"},
               "revision": {"session_revision": secrets.token_hex(32)},
               "role": {"roles": frozenset()}, "enabled": {"enabled": False}}
    accounts[USER] = replace(accounts[USER], **changes[change])
    _lock_untrusted(identity)
    before = identity.local_settings.authenticate.call_count
    with pytest.raises(LocalAuthenticationError):
        _sign_in(identity, cookie=cookie)
    assert identity.local_settings.authenticate.call_count == before


@pytest.mark.parametrize("kind", ["forged", "tampered", "session-key", "challenge", "malformed"])
def test_device_cookie_forged_or_tampered_or_session_key_signed_is_untrusted(lane_identity, kind):
    identity, _, _ = lane_identity
    original = _sign_in(identity).device_cookie
    version, device_id, signature = original.split(".")
    if kind == "forged":
        device_id = secrets.token_hex(16)
    elif kind == "tampered":
        signature = ("0" if signature[0] != "0" else "1") + signature[1:]
    elif kind == "session-key":
        username_digest = identity._local_devices.username_digest(USER)
        signature = hmac.new(identity._secret, f"device-cookie|{device_id}|{username_digest}".encode(),
                             hashlib.sha256).hexdigest()
    cookie = f"{version}.{device_id}.{signature}"
    if kind == "challenge":
        cookie = identity.new_login_challenge()
    elif kind == "malformed":
        cookie = original + ".unexpected"
    _lock_untrusted(identity)
    before = identity.local_settings.authenticate.call_count
    with pytest.raises(LocalAuthenticationError):
        _sign_in(identity, cookie=cookie)
    assert identity.local_settings.authenticate.call_count == before
    assert _sign_in(identity, cookie=original).device_cookie


def test_device_lane_limit_and_lifetime_revocation(lane_identity):
    identity, clock, _ = lane_identity
    cookie = _sign_in(identity).device_cookie
    for batch in range(3):
        for _ in range(10):
            assert _failure(identity, cookie=cookie).retry_after_seconds is None
        if batch < 2:
            before = identity.local_settings.authenticate.call_count
            error = _failure(identity, cookie=cookie)
            assert error.retry_after_seconds == 900
            assert identity.local_settings.authenticate.call_count == before
            clock.advance(minutes=15)
    row = _rows(identity)[0]
    assert row["failures"] == 30 and row["state"] == "revoked"
    assert not identity._local_limiter.usernames
    _lock_untrusted(identity)
    with pytest.raises(LocalAuthenticationError):
        _sign_in(identity, cookie=cookie)


def test_device_success_does_not_clear_untrusted_aggregate(lane_identity):
    identity, clock, _ = lane_identity
    cookie = _sign_in(identity).device_cookie
    _lock_untrusted(identity)
    clock.advance(minutes=16)
    _lock_untrusted(identity)
    clock.advance(minutes=16)
    for _ in range(50):
        _failure(identity, username=OTHER)
    before = [(window.count, dict(window.slots)) for window in identity._local_limiter.daily.values()]
    assert _sign_in(identity, cookie=cookie).device_cookie
    assert [(window.count, dict(window.slots)) for window in identity._local_limiter.daily.values()] == before
    with pytest.raises(LocalAuthenticationError):
        _sign_in(identity)


def test_shared_pc_cookie_for_user_a_does_not_help_user_b(lane_identity):
    identity, _, _ = lane_identity
    first = _sign_in(identity).device_cookie
    both = _sign_in(identity, username=OTHER, cookie=first).device_cookie
    assert len(both.split("~")) == 2 and both.split("~")[1] == first
    _lock_untrusted(identity, OTHER)
    with pytest.raises(LocalAuthenticationError):
        _sign_in(identity, username=OTHER, cookie=first)
    rotated = _sign_in(identity, username=OTHER, cookie=both).device_cookie
    assert rotated.split("~")[1] == first


def test_device_cookie_attributes(login_handler):
    _, login = login_handler
    response = login()
    cookie = SimpleCookie()
    for header in response.headers.getlist("set-cookie"):
        cookie.load(header)
    value = cookie[DEVICE_COOKIE]
    assert DEVICE_COOKIE.startswith("__Host-")
    assert value["secure"] and value["httponly"]
    assert value["samesite"] == "strict" and value["path"] == "/"
    assert not value["domain"] and int(value["max-age"]) == DEVICE_MAX_AGE
    development = Response()
    set_device_cookie(development, value.value, secure=False)
    assert "set-cookie" not in development.headers


def test_ipv6_clients_in_same_64_share_client_budget(lane_identity):
    identity, _, _ = lane_identity
    for number in range(10):
        _failure(identity, address=f"2001:db8:1:2::{number + 1:x}")
    before = identity.local_settings.authenticate.call_count
    with pytest.raises(LocalAuthenticationError):
        _sign_in(identity, address="2001:db8:1:2:ffff::1")
    assert identity.local_settings.authenticate.call_count == before
    assert _sign_in(identity, address="2001:db8:1:3::1")[1]
    for _ in range(10):
        _failure(identity, address="::ffff:192.0.2.30")
    with pytest.raises(LocalAuthenticationError):
        _sign_in(identity, address="192.0.2.30")
    assert normalize_client_key("unknown") == "unknown"


def test_untrusted_lane_24h_cap(lane_identity):
    identity, clock, _ = lane_identity
    for _ in range(2):
        _lock_untrusted(identity)
        clock.advance(minutes=16)
    assert identity.local_settings.authenticate.call_count == 200
    error = _failure(identity)
    assert error.retry_after_seconds == 24 * 3600 + 15 * 60 - 32 * 60
    assert identity.local_settings.authenticate.call_count == 200
    clock.advance(hours=23, minutes=42, seconds=59)
    assert _failure(identity).retry_after_seconds == 1
    clock.advance(seconds=1)
    assert _failure(identity).retry_after_seconds is None
    assert identity.local_settings.authenticate.call_count == 201


def test_daily_counter_memory_bound():
    limiter, clock = LocalLoginLimiter(), Clock()
    limiter.username_bucket_count = 7
    limiter.client_capacity = 3
    for index in range(3000):
        with limiter.attempt(str(index), format(index % 70, "x"), clock) as attempt:
            attempt.outcome = "failure"
        clock.advance(minutes=2)
        assert len(limiter.daily) <= 7
        assert all(len(window.slots) <= 97 and window.count <= 200 for window in limiter.daily.values())
        assert all(isinstance(slot, int) and isinstance(count, int)
                   for window in limiter.daily.values() for slot, count in window.slots.items())


def test_throttled_untrusted_attempt_never_verifies_password(lane_identity):
    identity, _, _ = lane_identity
    for _ in range(10):
        _failure(identity)
    before = identity.local_settings.authenticate.call_count
    with pytest.raises(LocalAuthenticationError) as error:
        _sign_in(identity)
    assert error.value.retry_after_seconds == 900
    assert identity.local_settings.authenticate.call_count == before


def test_unknown_username_follows_same_lane_and_cost_path(lane_identity):
    identity, _, _ = lane_identity
    settings = LocalAccountSettings.__new__(LocalAccountSettings)
    account = LocalAccount(USER, "Synthetic Account", PasswordHasher().hash(PASSWORD))
    settings.account = Mock(side_effect=lambda username: account if username == USER else None)
    settings._dummy_hash = PasswordHasher().hash(secrets.token_urlsafe(32))
    settings._hasher = Mock(wraps=PasswordHasher())
    identity.local_settings = settings
    for username in (USER, "synthetic.absent"):
        for _ in range(10):
            assert str(_failure(identity, username=username)) == "The username or password is incorrect."
        before = settings._hasher.verify.call_count
        with pytest.raises(LocalAuthenticationError):
            _sign_in(identity, username=username)
        assert settings._hasher.verify.call_count == before
    assert settings._hasher.verify.call_count == 20
    hashes = [call.args[0] for call in settings._hasher.verify.call_args_list]
    assert hashes[:10] == [settings.account(USER).password_hash] * 10
    assert hashes[10:] == [settings._dummy_hash] * 10


def test_throttled_request_never_sleeps(login_handler, monkeypatch):
    identity, login = login_handler
    _lock_untrusted(identity)
    monkeypatch.setattr(time, "sleep", Mock(side_effect=AssertionError("request must not sleep")))
    assert login().headers["retry-after"] == "900"


@pytest.fixture
def registry(tmp_path):
    return LocalLoginDevices(tmp_path / "devices"), Clock()


def _registry_success(registry, cookie=None, username=USER):
    devices, clock = registry
    digest = devices.username_digest(username)
    revision = ("synthetic-revision", "synthetic-binding")
    with devices.attempt(cookie, digest, revision, clock) as result:
        result.outcome = "success"
        result.revision = revision
    return result.cookie


def test_device_registry_size_bound_and_expiry(registry):
    devices, clock = registry
    assert devices.capacity == 10_000
    devices.capacity = 8
    old = _registry_success(registry)
    current = _registry_success(registry, old)
    for index in range(40):
        _registry_success(registry, username=f"synthetic.user{index}")
        with devices._transaction() as db:
            assert db.execute("SELECT count(*) FROM devices").fetchone()[0] <= 8
        assert len(devices.windows) <= 8
    with devices._transaction() as db:
        assert devices._row(db, old) is None
    clock.advance(days=91)
    replacement = _registry_success(registry, current)
    with devices._transaction() as db:
        assert db.execute("SELECT count(*) FROM devices").fetchone()[0] == 1
        assert devices._row(db, replacement)["state"] == "active"
    assert not devices.windows


def test_device_registry_evicts_rotated_before_active(registry):
    devices, _ = registry
    devices.capacity = 3
    preserved = _registry_success(registry, username=OTHER)
    old = _registry_success(registry)
    new = _registry_success(registry, old)
    _registry_success(registry, username="synthetic.third")
    with devices._transaction() as db:
        assert devices._row(db, preserved)["state"] == "active"
        assert devices._row(db, old) is None
        assert devices._row(db, new)["state"] == "active"


def test_device_registry_cookie_entry_bound(registry):
    devices, clock = registry
    cookie = None
    for index in range(8):
        cookie = _registry_success(registry, cookie, f"synthetic.user{index}")
        assert len(cookie.split("~")) <= 5 and len(cookie) <= 504
    with devices.attempt(cookie + "~" + "malformed" * 1000,
                         devices.username_digest("synthetic.user7"),
                         ("synthetic-revision", "synthetic-binding"), clock) as attempt:
        assert attempt.device_digest is not None
    assert len(devices._entries(cookie + "~" + cookie)) == 5
    with devices.attempt("malformed~" + cookie, devices.username_digest("synthetic.user7"),
                         ("synthetic-revision", "synthetic-binding"), clock) as attempt:
        assert attempt.device_digest is not None


@pytest.mark.parametrize("filename", ["identity-device.key", "login-devices.sqlite3"])
@pytest.mark.parametrize("dangling", [False, True])
def test_device_registry_refuses_symlinks(tmp_path, filename, dangling):
    root = tmp_path / "runtime"
    root.mkdir()
    target = tmp_path / "synthetic-target"
    if not dangling:
        target.write_bytes(b"synthetic untouched contents")
        target.chmod(0o600)
    (root / filename).symlink_to(target)
    with pytest.raises(RuntimeError, match="unsafe"):
        LocalLoginDevices(root)
    if not dangling:
        assert target.read_bytes() == b"synthetic untouched contents"
    else:
        assert not target.exists()


def test_device_registry_permissions_restart_and_clean_restore(registry, tmp_path):
    devices, clock = registry
    old = _registry_success(registry)
    new = _registry_success(registry, old)
    for filename in ("identity-device.key", "login-devices.sqlite3"):
        assert (devices.path.parent / filename).stat().st_mode & 0o777 == 0o600
    with devices.attempt(new, devices.username_digest(USER),
                         ("synthetic-revision", "synthetic-binding"), clock) as attempt:
        attempt.outcome = "failure"
    backup = tmp_path / "backup"
    shutil.copytree(devices.path.parent, backup)
    shutil.rmtree(devices.path.parent)
    restored_path = tmp_path / "restored"
    shutil.copytree(backup, restored_path)
    restored = LocalLoginDevices(restored_path)
    assert not restored.windows
    with restored._transaction() as db:
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert restored._row(db, new)["failures"] == 1
        assert restored._row(db, old)["state"] == "rotated"
    with restored.attempt(new, restored.username_digest(USER),
                          ("synthetic-revision", "synthetic-binding"), clock) as attempt:
        assert attempt.device_digest is not None
    with restored.attempt(old, restored.username_digest(USER),
                          ("synthetic-revision", "synthetic-binding"), clock) as attempt:
        assert attempt.replayed and attempt.device_digest is None
    with restored._transaction() as db:
        assert restored._row(db, new)["state"] == "revoked"


def test_device_key_is_separate_from_session_key(lane_identity):
    identity, _, _ = lane_identity
    assert identity._local_devices._key != identity._secret
    assert (identity.runtime_dir / "identity-device.key").read_bytes() != (
        identity.runtime_dir / "identity-session.key").read_bytes()


def test_device_pending_checks_are_bounded_and_errors_release(registry):
    devices, clock = registry
    cookie = _registry_success(registry)
    digest = devices.username_digest(USER)
    revision = ("synthetic-revision", "synthetic-binding")
    with ExitStack() as pending:
        for _ in range(10):
            pending.enter_context(devices.attempt(cookie, digest, revision, clock))
        with pytest.raises(LoginThrottled):
            with devices.attempt(cookie, digest, revision, clock):
                pytest.fail("throttled device admitted")
        clock.advance(minutes=16)
        with pytest.raises(LoginThrottled):
            with devices.attempt(cookie, digest, revision, clock):
                pytest.fail("pending device reservation expired")
    with devices.attempt(cookie, digest, revision, clock) as attempt:
        assert attempt.device_digest
    assert all(window.pending == 0 for window in devices.windows.values())


def test_device_concurrent_rotation_never_revives_replayed_chain(registry):
    devices, clock = registry
    cookie = _registry_success(registry)
    digest = devices.username_digest(USER)
    revision = ("synthetic-revision", "synthetic-binding")
    with pytest.raises(RuntimeError, match="changed during sign-in"):
        with devices.attempt(cookie, digest, revision, clock) as pending:
            current = _registry_success(registry, cookie)
            pending.outcome = "success"
            pending.revision = revision
    assert pending.replayed and pending.cookie is None
    with devices._transaction() as db:
        assert devices._row(db, current)["state"] == "revoked"


def test_device_lifetime_pending_reservations_cannot_exceed_30(registry):
    devices, clock = registry
    cookie = _registry_success(registry)
    digest = devices.username_digest(USER)
    revision = ("synthetic-revision", "synthetic-binding")
    for index in range(29):
        with devices.attempt(cookie, digest, revision, clock) as result:
            result.outcome = "failure"
        clock.advance(minutes=16)
    with devices.attempt(cookie, digest, revision, clock) as pending:
        with pytest.raises(LoginThrottled):
            with devices.attempt(cookie, digest, revision, clock):
                pytest.fail("lifetime budget exceeded")
        pending.outcome = "failure"
    with devices._transaction() as db:
        row = devices._row(db, cookie)
        assert row["failures"] == 30 and row["state"] == "revoked"


def _gateway_request(peer=GATEWAY_PEER[0], values=("203.0.113.66",)):
    return Request({"type": "http", "client": (peer, 41000),
                    "headers": [(b"x-forwarded-for", value.encode()) for value in values]})


@pytest.fixture
def trust_environment(monkeypatch):
    for name in ("EXCULPATA_LOCAL_LOGIN_TRUST_GATEWAY", "RECORDBENCH_LOCAL_LOGIN_TRUST_GATEWAY",
                 "EXCULPATA_ALLOW_CONTAINER_BIND", "RECORDBENCH_ALLOW_CONTAINER_BIND"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(workbench, "_LOGIN_TRUST_WARNINGS", {})


def test_named_gateway_trust_setting_and_bind_separation(monkeypatch, trust_environment, caplog):
    resolve = Mock(return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", GATEWAY_PEER)])
    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    monkeypatch.setenv("EXCULPATA_ALLOW_CONTAINER_BIND", "1")
    for _ in range(3):
        assert workbench._local_login_client_key(_gateway_request()) == GATEWAY_PEER[0]
    assert not resolve.called
    assert sum("trust is unset" in record.message for record in caplog.records) == 1
    monkeypatch.setenv("EXCULPATA_LOCAL_LOGIN_TRUST_GATEWAY", "1")
    assert workbench._local_login_client_key(_gateway_request()) == "203.0.113.66"
    assert resolve.call_args.args[:2] == ("gateway", None)
    resolve.reset_mock()
    assert workbench._local_login_client_key(_gateway_request(), ()) == GATEWAY_PEER[0]
    assert not resolve.called


def test_deprecated_gateway_trust_alias(monkeypatch, trust_environment):
    monkeypatch.setenv("RECORDBENCH_LOCAL_LOGIN_TRUST_GATEWAY", "1")
    monkeypatch.setattr(socket, "getaddrinfo", Mock(return_value=[
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", GATEWAY_PEER)]))
    assert workbench._local_login_client_key(_gateway_request()) == "203.0.113.66"


@pytest.mark.parametrize("kind", ["resolve", "malformed", "missing", "untrusted"])
def test_gateway_warnings_are_rate_limited_and_content_free(monkeypatch, trust_environment, caplog, kind):
    monkeypatch.setenv("EXCULPATA_LOCAL_LOGIN_TRUST_GATEWAY", "1")
    seconds = [0.0]
    monkeypatch.setattr(workbench, "monotonic", lambda: seconds[0])
    secret_header = "203.0.113.66, 198.51.100.7"
    resolve = Mock(side_effect=socket.gaierror(secret_header))
    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    peers = None if kind == "resolve" else (GATEWAY_PEER[0],)
    request = _gateway_request(
        peer="192.0.2.11" if kind == "untrusted" else GATEWAY_PEER[0],
        values=() if kind in {"missing", "resolve"} else (secret_header,))
    for _ in range(10):
        assert workbench._local_login_client_key(request, peers) == request.client.host
    assert len(caplog.records) == 1
    seconds[0] = 299.9
    workbench._local_login_client_key(request, peers)
    assert len(caplog.records) == 1
    seconds[0] = 300
    workbench._local_login_client_key(request, peers)
    assert len(caplog.records) == 2
    assert all(value not in caplog.text for value in
               ("203.0.113.66", "198.51.100.7", "192.0.2.11", GATEWAY_PEER[0], USER))


def test_gateway_warning_rate_limit_is_thread_safe(monkeypatch, trust_environment, caplog):
    monkeypatch.setenv("EXCULPATA_LOCAL_LOGIN_TRUST_GATEWAY", "1")
    request = _gateway_request("192.0.2.11")
    with ThreadPoolExecutor(max_workers=12) as pool:
        list(pool.map(lambda _: workbench._local_login_client_key(request, (GATEWAY_PEER[0],)), range(100)))
    assert len(caplog.records) == 1


def test_compose_enables_gateway_trust_only_for_app():
    import yaml

    services = yaml.safe_load((Path(__file__).resolve().parents[1] / "compose.yaml").read_text())["services"]
    enabled = [name for name, service in services.items()
               if service.get("environment", {}).get("EXCULPATA_LOCAL_LOGIN_TRUST_GATEWAY") == "1"]
    assert enabled == ["app"]


def test_device_failed_admission_commit_does_not_leak_reservation(lane_identity, monkeypatch):
    identity, _, _ = lane_identity
    cookie = _sign_in(identity).device_cookie
    devices = identity._local_devices
    original = devices._transaction
    fail = [True]

    @contextmanager
    def transaction():
        with original() as db:
            yield db
            if fail[0]:
                fail[0] = False
                raise sqlite3.OperationalError("synthetic commit failure")

    monkeypatch.setattr(devices, "_transaction", transaction)
    before = identity.local_settings.authenticate.call_count
    with pytest.raises(LocalAuthenticationError, match="^Local account sign-in is temporarily unavailable\\.$"):
        _sign_in(identity, cookie=cookie)
    assert identity.local_settings.authenticate.call_count == before
    assert all(window.pending == 0 for window in devices.windows.values())
    assert _sign_in(identity, cookie=cookie).device_cookie


def test_daily_cap_reserves_pending_attempts_and_keeps_completion_time():
    limiter, clock = LocalLoginLimiter(), Clock()
    for batch, count in enumerate((100, 99)):
        for index in range(count):
            with limiter.attempt(str(index), "0", clock) as result:
                result.outcome = "failure"
        clock.advance(minutes=16)
    with limiter.attempt("fresh", "0", clock) as pending:
        with pytest.raises(LoginThrottled) as error:
            with limiter.attempt("another", "0", clock):
                pytest.fail("daily pending capacity exceeded")
        assert error.value.retry_after_seconds > 15 * 60
        clock.advance(hours=25)
        pending.outcome = "failure"
    daily = limiter.daily[0]
    assert daily.count == 1 and daily.pending == 0
    assert list(daily.slots) == [int(clock().timestamp() // 900)]


def test_device_failures_verify_concurrently_with_reserved_capacity(lane_identity, monkeypatch):
    identity, _, _ = lane_identity
    cookie = _sign_in(identity).device_cookie
    entered = threading.Barrier(10)
    verified = []

    def fail(username, password):
        entered.wait(timeout=5)
        verified.append(True)
        return None

    monkeypatch.setattr(identity.local_settings, "authenticate", fail)
    with ThreadPoolExecutor(max_workers=10) as pool:
        list(pool.map(lambda _: _failure(identity, cookie=cookie), range(10)))
    assert len(verified) == 10
    with pytest.raises(LocalAuthenticationError) as error:
        _sign_in(identity, cookie=cookie)
    assert error.value.retry_after_seconds == 900
    assert _rows(identity)[0]["failures"] == 10


def test_device_throttle_handler_returns_retry_after(login_handler):
    identity, login = login_handler
    cookie = _response_device(login())
    for _ in range(10):
        _failure(identity, cookie=cookie)
    before = identity.local_settings.authenticate.call_count
    response = login(cookie)
    assert response.status_code == 303 and response.headers["retry-after"] == "900"
    assert "Try again after 00:15 UTC." in unquote_plus(response.headers["location"])
    assert identity.local_settings.authenticate.call_count == before


def test_wrong_password_handler_message_and_headers_are_unchanged(login_handler):
    _, login = login_handler
    response = login(password="synthetic-wrong-password")
    assert response.status_code == 303
    assert "The username or password is incorrect." in unquote_plus(response.headers["location"])
    assert "retry-after" not in response.headers


def test_device_verifier_error_does_not_consume_failure_budget(lane_identity, monkeypatch):
    identity, _, _ = lane_identity
    cookie = _sign_in(identity).device_cookie
    monkeypatch.setattr(identity.local_settings, "authenticate", Mock(side_effect=RuntimeError("synthetic failure")))
    for _ in range(12):
        with pytest.raises(LocalAuthenticationError, match="^Local account sign-in is temporarily unavailable\\.$"):
            _sign_in(identity, cookie=cookie)
    assert _rows(identity)[0]["failures"] == 0
    assert all(window.count == 0 for window in identity._local_devices.windows.values())


def test_shared_cookie_replay_revokes_only_affected_chain(registry):
    devices, clock = registry
    first = _registry_success(registry)
    shared = _registry_success(registry, first, OTHER)
    current = _registry_success(registry, shared, USER)
    with devices.attempt(shared, devices.username_digest(OTHER),
                         ("synthetic-revision", "synthetic-binding"), clock) as attempt:
        assert attempt.replayed and attempt.device_digest is None
    with devices._transaction() as db:
        assert devices._row(db, current.split("~")[0])["state"] == "revoked"
        assert devices._row(db, shared.split("~")[0])["state"] == "active"


def test_retry_guidance_rounds_display_up_and_uses_all_blocking_windows():
    limiter, clock = LocalLoginLimiter(), Clock()
    clock.advance(seconds=30)
    for _ in range(10):
        with limiter.attempt("synthetic-client", "0", clock) as attempt:
            attempt.outcome = "failure"
    clock.advance(minutes=5)
    for index in range(90):
        with limiter.attempt(f"synthetic-client-{index}", "0", clock) as attempt:
            attempt.outcome = "failure"
    with pytest.raises(LoginThrottled) as error:
        with limiter.attempt("synthetic-client", "0", clock):
            pytest.fail("blocked admission")
    assert error.value.retry_after_seconds == 600
    assert str(error.value).endswith("Try again after 00:16 UTC.")
    clock.advance(minutes=10)
    with limiter.attempt("synthetic-client", "0", clock) as attempt:
        attempt.outcome = "failure"
    assert limiter.usernames[0].count == 91


def test_duplicate_username_cookie_entries_select_only_the_first(registry):
    devices, _ = registry
    first = _registry_success(registry)
    second = _registry_success(registry)
    current = _registry_success(registry, first + "~" + second)
    with devices._transaction() as db:
        assert devices._row(db, first)["state"] == "rotated"
        assert devices._row(db, second)["state"] == "active"
        assert devices._row(db, current)["chain_id"] == devices._row(db, first)["chain_id"]


def test_shared_replay_untrusted_success_still_rotates_submitted_users_entry(registry):
    devices, _ = registry
    first = _registry_success(registry)
    shared = _registry_success(registry, first, OTHER)
    _registry_success(registry, shared, USER)
    replacement = _registry_success(registry, shared, OTHER)
    with devices._transaction() as db:
        old_other = devices._row(db, shared.split("~")[0])
        assert old_other["state"] == "rotated"
        assert devices._row(db, replacement)["chain_id"] == old_other["chain_id"]
