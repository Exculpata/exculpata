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
from urllib.parse import parse_qs, urlsplit

import pytest
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from fastapi.testclient import TestClient
from starlette.requests import Request
from starlette.responses import Response

from case_intelligence import workbench
from case_intelligence.generation import UnavailableGenerator
from case_intelligence.identity import (
    IdentityService, LocalAccountSettings, LocalAuthenticationError, LocalLoginThrottledError,
    LOGIN_CHALLENGE_COOKIE, SESSION_COOKIE,
)
from case_intelligence.local_account_format import LocalAccount
from case_intelligence.local_login_devices import (
    DEVICE_COOKIE, DEVICE_MAX_AGE, LocalLoginDevices, set_device_cookie,
)
from case_intelligence.local_login_limiter import LocalLoginLimiter, LoginThrottled, normalize_client_key
from case_intelligence.login_errors import LOGIN_ERRORS, login_error_code, login_error_feedback, login_error_message
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


def test_throttled_redirect_maps_to_local_throttled_code(tmp_path):
    settings, _ = _settings()
    app = create_workbench_app(
        tmp_path / "runtime", generator=UnavailableGenerator(), auth_mode="local",
        secure_cookie=True, local_settings=settings, local_gateway_peers=(GATEWAY_PEER[0],),
        answer_workers=1)
    app.state.identity._clock = Clock()
    base = "https://exculpata.example.test"
    with ExitStack() as clients:
        for number in range(1, 11):
            attacker = clients.enter_context(
                TestClient(app, base_url=base, client=GATEWAY_PEER))
            for _ in range(10):
                _assert_denied(
                    _login(attacker, "synthetic-wrong-password", f"203.0.113.{number}"),
                    attacker)

        fresh = clients.enter_context(TestClient(app, base_url=base, client=GATEWAY_PEER))
        refused = _login(fresh, PASSWORD, "198.51.100.99")
        _assert_denied(refused, fresh)
        assert SESSION_COOKIE not in refused.cookies
        assert int(refused.headers["retry-after"]) > 0
        location = refused.headers["location"]
        destination = urlsplit(location)
        assert destination.path == "/auth/login"
        assert parse_qs(destination.query, keep_blank_values=True)["error"] == ["local_throttled"]

        page = fresh.get(location)
        assert page.status_code == 200
        minutes = int(parse_qs(destination.query)["retry_minutes"][0])
        assert LOGIN_ERRORS["local_throttled"] in page.text
        assert f"Time remaining: about {minutes} minutes." in page.text

    assert login_error_code(LocalLoginThrottledError(
        "Sign-in is temporarily unavailable. Try again in 3 minutes.",
        retry_after_seconds=150)) == "local_throttled"


def test_throttled_login_page_shows_bounded_retry_minutes(tmp_path):
    settings, _ = _settings()
    app = create_workbench_app(
        tmp_path / "runtime", generator=UnavailableGenerator(), auth_mode="local",
        secure_cookie=True, local_settings=settings, local_gateway_peers=(GATEWAY_PEER[0],),
        answer_workers=1)
    identity = app.state.identity
    identity._clock = Clock()
    base = "https://exculpata.example.test"
    with TestClient(app, base_url=base, client=GATEWAY_PEER) as fresh:
        _lock_untrusted(identity)
        identity._clock.advance(seconds=1)
        refused = _login(fresh, PASSWORD, "198.51.100.99")
        _assert_denied(refused, fresh)
        assert SESSION_COOKIE not in refused.cookies
        seconds = int(refused.headers["retry-after"])
        assert seconds > 0
        location = refused.headers["location"]
        destination = urlsplit(location)
        assert destination.path == "/auth/login"
        query = parse_qs(destination.query, keep_blank_values=True)
        assert query["error"] == ["local_throttled"]
        assert len(query.get("retry_minutes", [])) == 1
        assert re.fullmatch(r"[0-9]+", query["retry_minutes"][0])
        minutes = int(query["retry_minutes"][0])
        assert minutes == (seconds + 59) // 60
        assert 1 <= minutes <= 1440

        for value, page in [
            (minutes, fresh.get(location)),
            *[(value, fresh.get("/auth/login", params={
                "error": "local_throttled", "retry_minutes": str(value)}))
              for value in (1, 1440)],
        ]:
            assert page.status_code == 200
            unit = "minute" if value == 1 else "minutes"
            expected = LOGIN_ERRORS["local_throttled"] + f" Time remaining: about {value} {unit}."
            assert re.findall(r'<div[^>]*role="alert"[^>]*>(.*?)</div>', page.text) == [expected]
            assert LOGIN_ERRORS["local_throttled"] in page.text

        invalid_values = [
            [], ["0"], ["1441"], ["99999"], ["-5"], ["+5"], ["5.0"], ["05x"],
            [" 5"], [""], ["9" * 300], ["\u0665"], ["5", "6"],
        ]
        for values in invalid_values:
            page = fresh.get("/auth/login", params=[
                ("error", "local_throttled"),
                *[("retry_minutes", value) for value in values],
            ])
            assert page.status_code == 200
            messages = re.findall(r'<div[^>]*role="alert"[^>]*>(.*?)</div>', page.text)
            assert messages == [LOGIN_ERRORS["local_throttled"]], values
            assert "Time remaining" not in page.text
            assert all(value not in messages[0] for value in values if value)

        for code, expected in [
            ("local_credentials", [LOGIN_ERRORS["local_credentials"]]),
            ("synthetic_unknown_error", []),
        ]:
            page = fresh.get("/auth/login", params={"error": code, "retry_minutes": "5"})
            assert page.status_code == 200
            assert re.findall(r'<div[^>]*role="alert"[^>]*>(.*?)</div>', page.text) == expected
            assert "Time remaining" not in page.text
            assert LOGIN_ERRORS["local_throttled"] not in page.text
            if not expected:
                assert all(message not in page.text for message in LOGIN_ERRORS.values())
                assert code not in page.text


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


@pytest.fixture
def real_lane_identity(tmp_path):
    settings = LocalAccountSettings(_accounts(tmp_path))
    store = WorkspaceStore(tmp_path / "runtime/workbench.sqlite")
    identity = IdentityService(store, tmp_path / "runtime", auth_mode="local", secure_cookie=True,
                               local_settings=settings, clock=Clock())
    try:
        yield identity
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
    assert parse_qs(urlsplit(response.headers["location"]).query)["error"] == ["local_throttled"]
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


def test_lost_rotation_response_keeps_device_lane_while_untrusted_locked(tmp_path):
    settings, _ = _settings()
    app = create_workbench_app(
        tmp_path / "runtime", generator=UnavailableGenerator(), auth_mode="local",
        secure_cookie=True, local_settings=settings, local_gateway_peers=(GATEWAY_PEER[0],),
        answer_workers=1)
    identity = app.state.identity
    clock = Clock()
    identity._clock = clock
    base = "https://exculpata.example.test"
    with ExitStack() as clients:
        victim = clients.enter_context(TestClient(app, base_url=base, client=GATEWAY_PEER))
        response = _login(victim, PASSWORD, ADDRESS)
        assert response.status_code == 303
        destination = response.headers["location"]
        assert not destination.startswith("/auth/login")
        assert victim.cookies.get(SESSION_COOKIE) is not None
        original = victim.cookies[DEVICE_COOKIE]
        del victim.cookies[SESSION_COOKIE]

        clock.advance(seconds=1)
        response = _login(victim, PASSWORD, ADDRESS)
        assert response.status_code == 303
        assert response.headers["location"] == destination
        assert victim.cookies.get(SESSION_COOKIE) is not None
        lost_successor = victim.cookies[DEVICE_COOKIE]
        assert lost_successor != original
        victim.cookies.clear()
        victim.cookies.set(
            DEVICE_COOKIE, original, domain="exculpata.example.test", path="/")

        for number in range(1, 11):
            attacker = clients.enter_context(
                TestClient(app, base_url=base, client=GATEWAY_PEER))
            for _ in range(10):
                _assert_denied(
                    _login(attacker, "synthetic-wrong-password", f"203.0.113.{number}"),
                    attacker)

        fresh = clients.enter_context(TestClient(app, base_url=base, client=GATEWAY_PEER))
        refused = _login(fresh, PASSWORD, "198.51.100.99")
        _assert_denied(refused, fresh)
        assert int(refused.headers["retry-after"]) > 0

        clock.advance(seconds=30)
        response = _login(victim, PASSWORD, ADDRESS)
        assert response.status_code == 303
        assert response.headers["location"] == destination
        assert victim.cookies.get(SESSION_COOKIE) is not None
        replacement = victim.cookies[DEVICE_COOKIE]
        assert replacement not in (original, lost_successor)
        assert victim.cookies[DEVICE_COOKIE] == replacement
        assert not any(event.action == "auth.device.replay"
                       for event in identity.store.audit_events())


def test_double_submit_with_same_device_cookie_keeps_chain_active(login_handler):
    identity, login = login_handler
    initial = login()
    destination = initial.headers["location"]
    assert not destination.startswith("/auth/login")
    original = _response_device(initial)
    chain = _rows(identity)[0]["chain_id"]
    identity._clock.advance(seconds=1)
    first, last = login(original), login(original)
    for response in (initial, first, last):
        assert response.status_code == 303
        assert response.headers["location"] == destination
        assert SESSION_COOKIE in response.headers["set-cookie"]
    first_successor, last_successor = _response_device(first), _response_device(last)
    assert len({original, first_successor, last_successor}) == 3
    rows = _rows(identity)
    assert all(row["chain_id"] == chain and row["state"] != "revoked" for row in rows)
    assert sum(row["state"] == "active" for row in rows) == 2
    _lock_untrusted(identity)
    identity._clock.advance(seconds=30)
    accepted = login(last_successor)
    assert accepted.status_code == 303 and accepted.headers["location"] == destination
    current = _response_device(accepted)
    assert current != last_successor
    refused = login(first_successor)
    # The deleted sibling uses the untrusted window, with 30 of its 900 seconds elapsed.
    assert refused.status_code == 303 and refused.headers["retry-after"] == "870"
    assert refused.headers["location"].startswith("/auth/login?")
    assert SESSION_COOKIE not in refused.headers.get("set-cookie", "")
    assert login(current).headers["location"] == destination
    assert all(row["chain_id"] == chain and row["state"] != "revoked" for row in _rows(identity))
    assert any(row["state"] == "active" for row in _rows(identity))
    assert not any(event.action == "auth.device.replay" for event in identity.store.audit_events())


def test_device_cookie_rotates_on_success_and_old_value_is_rejected(lane_identity):
    identity, _, _ = lane_identity
    old = _sign_in(identity).device_cookie
    new = _sign_in(identity, cookie=old).device_cookie
    assert old != new
    rows = _rows(identity)
    assert [row["state"] for row in rows] == ["rotated", "active"]
    assert rows[0]["chain_id"] == rows[1]["chain_id"]
    assert old.split(".")[1] not in repr(rows)
    _failure(identity, cookie=new)
    _lock_untrusted(identity)
    with pytest.raises(LocalAuthenticationError) as error:
        _sign_in(identity, cookie=old)
    assert error.value.retry_after_seconds == 900
    assert error.value.device_replayed
    with pytest.raises(LocalAuthenticationError):
        _sign_in(identity, cookie=new)


@pytest.mark.parametrize("locked", [False, True])
@pytest.mark.parametrize("successor_succeeds", [False, True])
def test_replayed_rotated_device_cookie_revokes_chain_and_audits(login_handler, locked, successor_succeeds):
    identity, login = login_handler
    old = _response_device(login())
    current = _response_device(login(old))
    if successor_succeeds:
        login(current)
    else:
        _failure(identity, cookie=current)
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
    with devices.attempt(cookie, digest, revision, clock) as pending:
        current = _registry_success(registry, cookie)
        with devices.attempt(current, digest, revision, clock):
            pass
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


@pytest.mark.parametrize("variant", ["retry-recovers", "release-recovers", "release-also-fails"])
@pytest.mark.parametrize("fault", [sqlite3.OperationalError, OSError, RuntimeError])
def test_successful_device_completion_failure_releases_pending(
        lane_identity, monkeypatch, caplog, variant, fault):
    identity, clock, _ = lane_identity
    cookie = _sign_in(identity).device_cookie
    devices = identity._local_devices
    original = devices._transaction
    with original() as db:
        # fixture-write: Leave one lifetime slot to expose a successful reservation counted as a failure.
        db.execute("UPDATE devices SET failures = 29")
        chain_id = devices._row(db, cookie)["chain_id"]
    transactions = 0

    @contextmanager
    def transaction():
        nonlocal transactions
        transactions += 1
        with original() as db:
            yield db
            if (transactions == 2
                    or (variant == "release-recovers" and transactions == 3)
                    or (variant == "release-also-fails" and transactions > 2)):
                raise fault("synthetic completion commit failure")

    with monkeypatch.context() as patch:
        patch.setattr(devices, "_transaction", transaction)
        result = _sign_in(identity, cookie=cookie)
    assert result[1]
    recovered = variant == "retry-recovers"
    assert bool(result.device_cookie) == recovered
    if recovered:
        with original() as db:
            rows = db.execute("SELECT * FROM devices WHERE chain_id = ?", (chain_id,)).fetchall()
            assert all(row["state"] != "revoked" and row["pending"] == 0 for row in rows)
            failures = {row["device_digest"]: row["failures"] for row in rows}
            assert devices._row(db, result.device_cookie)["state"] == "active"
    clock.advance(seconds=devices.pending_timeout_seconds + 1)
    with devices._lock, original() as db:
        devices._maintain(db, clock())
        rows = db.execute("SELECT * FROM devices WHERE chain_id = ?", (chain_id,)).fetchall()
        assert all(row["state"] != "revoked" and row["pending"] == 0 for row in rows)
        assert any(row["state"] == "active" for row in rows)
        if recovered:
            assert {row["device_digest"]: row["failures"] for row in rows} == failures
            assert all(row["failures"] == 0 for row in rows)
        else:
            assert all(row["state"] == "active" and row["failures"] == 29 for row in rows)
    assert transactions == {"retry-recovers": 3, "release-recovers": 4, "release-also-fails": 5}[variant]
    warnings = [record for record in caplog.records if record.levelname == "WARNING"]
    if recovered:
        assert not warnings
    else:
        assert len(warnings) == 1
        assert warnings[0].message == (
            "Local login device completion failed; sign-in succeeded without a device cookie.")
    assert all(value not in caplog.text for value in (cookie, USER, ADDRESS))
    assert all(row[key] not in caplog.text for row in rows for key in ("device_digest", "username_digest"))


@pytest.mark.parametrize("replayed", [False, True])
def test_successful_device_completion_retry_restores_attempt_state(registry, monkeypatch, caplog, replayed):
    devices, clock = registry
    cookie = _registry_success(registry)
    original = devices._issue
    calls = 0

    def issue(db, attempt, cookie, username, now):
        nonlocal calls
        calls += 1
        assert attempt.cookie is None and attempt.replayed == replayed
        original(db, attempt, cookie, username, now)
        if calls == 1:
            attempt.replayed = not replayed
            raise RuntimeError("synthetic issuance failure after setting attempt state")

    monkeypatch.setattr(devices, "_issue", issue)
    revision = ("synthetic-revision", "synthetic-binding")
    with devices.attempt(cookie, devices.username_digest(USER), revision, clock) as attempt:
        attempt.outcome, attempt.revision, attempt.replayed = "success", revision, replayed
    assert calls == 2
    assert attempt.cookie and attempt.replayed == replayed
    assert not [record for record in caplog.records if record.levelname == "WARNING"]
    with devices._transaction() as db:
        assert devices._row(db, attempt.cookie)["state"] == "active"


def test_successful_pending_releases_preserve_other_reservations(registry, monkeypatch):
    devices, clock = registry
    cookie = _registry_success(registry)
    digest = devices.username_digest(USER)
    revision = ("synthetic-revision", "synthetic-binding")
    original = devices._transaction
    with original() as db:
        # fixture-write: Reserve three lifetime slots for two successes and one abandoned attempt.
        db.execute("UPDATE devices SET failures = 27")

    @contextmanager
    def failed_commit():
        with original() as db:
            yield db
            raise sqlite3.OperationalError("synthetic completion commit failure")

    with devices.attempt(cookie, digest, revision, clock):
        with monkeypatch.context() as patch:
            with ExitStack() as successes:
                for _ in range(2):
                    attempt = successes.enter_context(devices.attempt(cookie, digest, revision, clock))
                    attempt.outcome, attempt.revision = "success", revision
                patch.setattr(devices, "_transaction", failed_commit)
        clock.advance(seconds=devices.pending_timeout_seconds + 1)
        for _ in range(2):
            with original() as db:
                devices._maintain(db, clock())
                row = devices._row(db, cookie)
                assert row["state"] == "active"
                assert row["failures"] == 28 and row["pending"] == 0
    assert all(window.pending == 0 for window in devices.windows.values())


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
    assert int(response.headers["retry-after"]) > 0
    assert parse_qs(urlsplit(response.headers["location"]).query)["error"] == ["local_throttled"]
    assert identity.local_settings.authenticate.call_count == before


def test_wrong_password_handler_message_and_headers_are_unchanged(login_handler):
    _, login = login_handler
    response = login(password="synthetic-wrong-password")
    assert response.status_code == 303
    query = parse_qs(urlsplit(response.headers["location"]).query)
    assert query["error"] == ["local_credentials"] and "retry_minutes" not in query
    assert LOGIN_ERRORS["local_credentials"] == "The username or password is incorrect."
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


@pytest.mark.parametrize("fault", [RuntimeError, VerificationError])
def test_real_device_verifier_fault_preserves_failure_budget(real_lane_identity, monkeypatch, fault):
    identity = real_lane_identity
    cookie = _sign_in(identity).device_cookie
    assert cookie
    with monkeypatch.context() as patch:
        patch.setattr(PasswordHasher, "verify", Mock(side_effect=fault("synthetic verifier fault")))
        for _ in range(31):
            with pytest.raises(LocalAuthenticationError, match=r"^Local account sign-in is temporarily unavailable\.$"):
                _sign_in(identity, cookie=cookie)
            rows = _rows(identity)
            assert rows
            assert all(row["failures"] == 0 and row["pending"] == 0 for row in rows)
            assert all(row["state"] != "revoked" for row in rows)
    assert _sign_in(identity, cookie=cookie).device_cookie


def test_real_device_invalid_hash_preserves_failure_budget(real_lane_identity, monkeypatch):
    identity = real_lane_identity
    cookie = _sign_in(identity).device_cookie
    assert cookie
    # The account repository rejects malformed hashes before verification.
    monkeypatch.setattr(PasswordHasher, "verify", Mock(side_effect=InvalidHashError()))
    with pytest.raises(LocalAuthenticationError, match=r"^Local account sign-in is temporarily unavailable\.$"):
        _sign_in(identity, cookie=cookie)
    rows = _rows(identity)
    assert rows
    assert all(row["failures"] == 0 and row["pending"] == 0 for row in rows)
    assert all(row["state"] != "revoked" for row in rows)


def test_real_device_wrong_password_consumes_failure_budget(real_lane_identity):
    identity = real_lane_identity
    cookie = _sign_in(identity).device_cookie
    assert cookie
    with pytest.raises(LocalAuthenticationError, match=r"^The username or password is incorrect\.$"):
        _sign_in(identity, cookie=cookie, password="synthetic-wrong-password")
    rows = _rows(identity)
    assert len(rows) == 1
    assert rows[0]["failures"] == 1 and rows[0]["pending"] == 0
    assert rows[0]["state"] == "active"


@pytest.mark.parametrize("disabled", [False, True])
def test_real_account_dummy_hash_mismatch_returns_none(real_lane_identity, monkeypatch, disabled):
    settings = real_lane_identity.local_settings
    account = replace(settings.account(USER), enabled=False) if disabled else None
    monkeypatch.setattr(settings, "account", lambda username: account)
    verify = Mock(wraps=settings._hasher.verify)
    monkeypatch.setattr(PasswordHasher, "verify", verify)
    assert settings.authenticate(USER, PASSWORD) is None
    verify.assert_called_once_with(settings._dummy_hash, PASSWORD)


def test_shared_cookie_replay_revokes_only_affected_chain(registry):
    devices, clock = registry
    first = _registry_success(registry)
    shared = _registry_success(registry, first, OTHER)
    current = _registry_success(registry, shared, USER)
    with devices.attempt(current, devices.username_digest(USER),
                         ("synthetic-revision", "synthetic-binding"), clock):
        pass
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
    assert str(error.value).endswith("Try again in 10 minutes.")
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
    devices, clock = registry
    first = _registry_success(registry)
    shared = _registry_success(registry, first, OTHER)
    current = _registry_success(registry, shared, USER)
    with devices.attempt(current, devices.username_digest(USER),
                         ("synthetic-revision", "synthetic-binding"), clock):
        pass
    replacement = _registry_success(registry, shared, OTHER)
    with devices._transaction() as db:
        old_other = devices._row(db, shared.split("~")[0])
        assert old_other["state"] == "rotated"
        assert devices._row(db, replacement)["chain_id"] == old_other["chain_id"]


@pytest.mark.parametrize(("seconds", "minutes", "unit"), [
    (0, 1, "minute"), (60, 1, "minute"), (60.1, 2, "minutes"), (121, 3, "minutes"),
    (86401, 1441, "minutes"),
])
def test_retry_guidance_uses_relative_rounded_minutes(login_handler, monkeypatch, seconds, minutes, unit):
    error = LoginThrottled(seconds, Clock()())
    assert str(error) == f"Sign-in is temporarily unavailable. Try again in {minutes} {unit}."
    assert isinstance(error.retry_after_seconds, int) and error.retry_after_seconds >= 1
    identity, login = login_handler
    refusal = LocalLoginThrottledError(str(error), retry_after_seconds=error.retry_after_seconds)
    monkeypatch.setattr(identity, "login_local", Mock(side_effect=refusal))
    response = login()
    query = parse_qs(urlsplit(response.headers["location"]).query)
    assert query["error"] == ["local_throttled"] and query["retry_minutes"] == [str(min(1440, minutes))]
    assert response.headers["retry-after"] == str(error.retry_after_seconds)


@pytest.mark.parametrize("subclass", [False, True])
@pytest.mark.parametrize("message", [
    "Sign-in is temporarily unavailable. Try again in 3 minutes.",
    LOGIN_ERRORS["local_credentials"],
    "Synthetic throttling refusal",
])
def test_throttled_error_code_uses_type_before_message(subclass, message):
    class SyntheticThrottledError(LocalLoginThrottledError):
        pass

    error_type = SyntheticThrottledError if subclass else LocalLoginThrottledError
    error = error_type(message, retry_after_seconds=150)
    assert login_error_code(error) == "local_throttled"
    assert str(error) == message
    assert error.retry_after_seconds == 150
    expected = "local_credentials" if message == LOGIN_ERRORS["local_credentials"] else "sign_in_failed"
    assert login_error_code(LocalAuthenticationError(message, retry_after_seconds=150)) == expected
    for codes in ([], ["local_credentials"], ["unknown"], ["local_throttled"] * 2):
        assert login_error_feedback(codes, ["5"]) == login_error_message(codes)
        assert "Time remaining" not in login_error_feedback(codes, ["5"])


def test_throttled_identity_has_dedicated_error_type(lane_identity):
    identity, _, _ = lane_identity
    for _ in range(10):
        _failure(identity)
    with pytest.raises(LocalLoginThrottledError) as error:
        _sign_in(identity)
    assert error.value.retry_after_seconds == 900
    assert str(error.value) == "Sign-in is temporarily unavailable. Try again in 15 minutes."
    assert login_error_code(error.value) == "local_throttled"


def test_device_per_username_cap_preserves_other_users_and_retires_lru(registry):
    devices, clock = registry
    assert devices.per_user_active_limit == 20
    devices.capacity = devices.per_user_active_limit + 3
    preserved = _registry_success(registry, username=OTHER)
    oldest = _registry_success(registry)
    clock.advance(seconds=1)
    recent = _registry_success(registry)
    clock.advance(seconds=1)
    with devices.attempt(oldest, devices.username_digest(USER),
                         ("synthetic-revision", "synthetic-binding"), clock):
        pass
    for index in range(devices.per_user_active_limit - 1):
        clock.advance(seconds=1)
        _registry_success(registry)
    with devices._transaction() as db:
        assert devices._row(db, oldest)["state"] == "active"
        assert devices._row(db, recent)["state"] == "revoked"
    for index in range(60):
        clock.advance(seconds=1)
        _registry_success(registry)
        with devices._transaction() as db:
            assert devices._row(db, preserved)["state"] == "active"
            count = db.execute("SELECT count(*) FROM devices WHERE username_digest = ? "
                               "AND state = 'active'", (devices.username_digest(USER),)).fetchone()[0]
            assert count <= devices.per_user_active_limit
            assert db.execute("SELECT count(*) FROM devices").fetchone()[0] <= devices.capacity
    devices.capacity = 100
    original = _registry_success(registry)
    current = _registry_success(registry, original)
    for _ in range(devices.per_user_active_limit - 1):
        _registry_success(registry)
    assert _registry_success(registry, original) is None
    with devices._transaction() as db:
        assert devices._row(db, current)["state"] == "revoked"


@pytest.mark.parametrize("failure", ["busy", "unavailable", "commit", "revoked"])
def test_successful_password_sign_in_survives_device_issuance_failure(login_handler, monkeypatch, failure):
    identity, login = login_handler
    cookie = _response_device(login())
    devices = identity._local_devices
    original_transaction = devices._transaction
    original_authenticate = identity.local_settings.authenticate.side_effect

    @contextmanager
    def unavailable():
        raise OSError("synthetic unavailable registry")
        yield

    @contextmanager
    def failed_commit():
        with original_transaction() as db:
            yield db
            raise sqlite3.OperationalError("synthetic commit failure")

    def authenticate(username, password):
        if failure == "revoked":
            with original_transaction() as db:
                # fixture-write: Revoke the device registry entry during password verification to exercise issuance losing a revocation race.
                db.execute("UPDATE devices SET state = 'revoked'")
        elif failure == "busy":
            monkeypatch.setattr(devices, "_prune", Mock(side_effect=RuntimeError("synthetic busy registry")))
        else:
            monkeypatch.setattr(devices, "_transaction", unavailable if failure == "unavailable" else failed_commit)
        return original_authenticate(username, password)

    monkeypatch.setattr(identity.local_settings, "authenticate", authenticate)
    response = login(cookie)
    assert response.status_code == 303
    assert not response.headers["location"].startswith("/auth/login?")
    cookies = SimpleCookie()
    for header in response.headers.getlist("set-cookie"):
        cookies.load(header)
    assert cookies[SESSION_COOKIE].value and DEVICE_COOKIE not in cookies
    assert all(window.pending == 0 for window in devices.windows.values())
    with original_transaction() as db:
        assert db.execute("SELECT count(*) FROM devices").fetchone()[0] == 1
        assert devices._row(db, cookie)["state"] == ("revoked" if failure == "revoked" else "active")


@pytest.mark.parametrize("elapsed", [0, 60])
def test_unit_lost_rotation_response_keeps_device_lane_while_untrusted_locked(
        login_handler, monkeypatch, elapsed):
    identity, login = login_handler
    monkeypatch.setattr("case_intelligence.local_login_devices.monotonic",
                        lambda: identity._clock().timestamp())
    original = _response_device(login())
    lost = _response_device(login(original))
    _lock_untrusted(identity)
    identity._clock.advance(seconds=elapsed)
    response = login(original)
    assert response.status_code == 303 and not response.headers["location"].startswith("/auth/login?")
    assert _response_device(response) not in (original, lost)
    assert not any(event.action == "auth.device.replay" for event in identity.store.audit_events())
    assert [row["state"] for row in _rows(identity)] == ["rotated", "active", "active"]


@pytest.mark.parametrize("elapsed,admit_last", [
    pytest.param(61, False, id="earlier-sibling-after-grace"),
    pytest.param(3600, False, id="earlier-sibling-after-hour"),
    pytest.param(0, True, id="later-sibling-admitted-first"),
])
def test_double_submit_keeping_earlier_sibling_survives_grace(
        login_handler, monkeypatch, elapsed, admit_last):
    identity, login = login_handler
    initial = login()
    original = _response_device(initial)
    destination = initial.headers["location"]
    chain = _rows(identity)[0]["chain_id"]
    identity._clock.advance(seconds=1)
    first = login(original)
    first_successor = _response_device(first)
    first_digest = _rows(identity)[-1]["device_digest"]
    last = login(original)
    last_successor = _response_device(last)
    assert len({original, first_successor, last_successor}) == 3
    for response in (initial, first, last):
        assert response.status_code == 303
        assert response.headers["location"] == destination
        assert not destination.startswith("/auth/login")
        cookies = SimpleCookie()
        for header in response.headers.getlist("set-cookie"):
            cookies.load(header)
        assert cookies[SESSION_COOKIE].value

    identity._clock.advance(seconds=elapsed)
    if admit_last:
        admitted = login(last_successor)
        assert admitted.status_code == 303
        assert admitted.headers["location"] == destination
        current = _response_device(admitted)
    else:
        _lock_untrusted(identity)
        refused = login(address="198.51.100.99")
        assert refused.headers["location"].startswith("/auth/login?")
        assert int(refused.headers["retry-after"]) > 0

    untrusted_attempt = Mock(wraps=identity._local_limiter.attempt)
    monkeypatch.setattr(identity._local_limiter, "attempt", untrusted_attempt)
    response = login(first_successor)
    assert not any(event.action == "auth.device.replay"
                   for event in identity.store.audit_events())
    assert response.status_code == 303
    assert response.headers["location"] == destination
    cookies = SimpleCookie()
    for header in response.headers.getlist("set-cookie"):
        cookies.load(header)
    assert cookies[SESSION_COOKIE].value
    assert untrusted_attempt.call_count == int(admit_last)
    rows = _rows(identity)
    assert all(row["state"] != "revoked" for row in rows)
    assert any(row["chain_id"] == chain and row["state"] == "active" for row in rows)

    if admit_last:
        assert all(row["device_digest"] != first_digest for row in rows)
        _lock_untrusted(identity)
        refused = login(address="198.51.100.99")
        assert refused.headers["location"].startswith("/auth/login?")
        assert int(refused.headers["retry-after"]) > 0
        untrusted_attempt.reset_mock()
        response = login(current)
        assert response.status_code == 303
        assert response.headers["location"] == destination
        cookies = SimpleCookie()
        for header in response.headers.getlist("set-cookie"):
            cookies.load(header)
        assert cookies[SESSION_COOKIE].value
        assert _response_device(response) != current
        untrusted_attempt.assert_not_called()
        rows = _rows(identity)
        assert all(row["state"] != "revoked" for row in rows)
        assert any(row["chain_id"] == chain and row["state"] == "active" for row in rows)
        assert not any(event.action == "auth.device.replay"
                       for event in identity.store.audit_events())


def test_rotated_entry_after_grace_revokes_unused_successor_and_audits(login_handler, monkeypatch):
    identity, login = login_handler
    monkeypatch.setattr("case_intelligence.local_login_devices.monotonic",
                        lambda: identity._clock().timestamp())
    original = _response_device(login())
    current = _response_device(login(original))
    identity._clock.advance(seconds=identity._local_devices.rotation_grace_seconds + 1)
    _lock_untrusted(identity)
    response = login(original)
    assert response.status_code == 303 and response.headers["retry-after"] == "900"
    assert all(row["state"] == "revoked" for row in _rows(identity))
    events = [event for event in identity.store.audit_events() if event.action == "auth.device.replay"]
    assert len(events) == 1 and events[0].outcome == "denied"
    assert current not in json.dumps(asdict(events[0]))


def test_device_inflight_double_submit_keeps_chain_active(registry):
    devices, clock = registry
    original = _registry_success(registry)
    digest = devices.username_digest(USER)
    revision = ("synthetic-revision", "synthetic-binding")
    with devices.attempt(original, digest, revision, clock) as pending:
        current = _registry_success(registry, original)
        pending.outcome = "success"
        pending.revision = revision
    assert pending.cookie not in (None, original, current) and not pending.replayed
    with devices._transaction() as db:
        rows = db.execute("SELECT state FROM devices").fetchall()
        assert sorted(row["state"] for row in rows) == ["active", "active", "rotated"]


def test_grace_retries_do_not_extend_original_rotation_deadline(registry, monkeypatch):
    devices, clock = registry
    monkeypatch.setattr("case_intelligence.local_login_devices.monotonic", lambda: clock().timestamp())
    original = _registry_success(registry)
    _registry_success(registry, original)
    clock.advance(seconds=50)
    current = _registry_success(registry, original)
    clock.advance(seconds=11)
    with devices.attempt(original, devices.username_digest(USER),
                         ("synthetic-revision", "synthetic-binding"), clock) as attempt:
        assert attempt.replayed and attempt.device_digest is None
    with devices._transaction() as db:
        assert devices._row(db, current)["state"] == "revoked"


def test_grace_entry_keeps_presented_device_failure_budget(lane_identity):
    identity, _, _ = lane_identity
    original = _sign_in(identity).device_cookie
    _sign_in(identity, cookie=original)
    for _ in range(10):
        assert _failure(identity, cookie=original).retry_after_seconds is None
    before = identity.local_settings.authenticate.call_count
    with pytest.raises(LocalLoginThrottledError):
        _sign_in(identity, cookie=original)
    assert identity.local_settings.authenticate.call_count == before
    assert _rows(identity)[0]["failures"] == 10


@pytest.mark.parametrize("change", ["username", "revision", "binding"])
def test_grace_entry_requires_matching_username_revision_and_binding(registry, change):
    devices, clock = registry
    original = _registry_success(registry)
    _registry_success(registry, original)
    digest = devices.username_digest(OTHER if change == "username" else USER)
    revision = ("changed" if change == "revision" else "synthetic-revision",
                "changed" if change == "binding" else "synthetic-binding")
    with devices.attempt(original, digest, revision, clock) as attempt:
        assert attempt.device_digest is None and not attempt.replayed


@pytest.mark.parametrize("columns", [("parent_digest",), ("admitted", "successor_used", "parent_digest")])
def test_device_registry_migrates_existing_table_and_preserves_restore(registry, tmp_path, columns):
    devices, clock = registry
    original = _registry_success(registry)
    current = _registry_success(registry, original)
    with devices._transaction() as db:
        for column in columns:
            # fixture-write: Remove each new column to construct the legacy device schema for migration and restore.
            db.execute(f"ALTER TABLE devices DROP COLUMN {column}")
    backup = tmp_path / "legacy-backup"
    shutil.copytree(devices.path.parent, backup)
    for root in (devices.path.parent, backup):
        restored = LocalLoginDevices(root)
        restored = LocalLoginDevices(root)
        with restored._transaction() as db:
            assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            assert restored._row(db, current)["state"] == "active"
            assert restored._row(db, current)["parent_digest"] is None
        with restored.attempt(original, restored.username_digest(USER),
                              ("synthetic-revision", "synthetic-binding"), clock) as attempt:
            assert attempt.replayed == ("admitted" in columns)
            assert (attempt.device_digest is None) == ("admitted" in columns)
        fresh = _registry_success((restored, clock))
        _registry_success((restored, clock), fresh)
        with restored.attempt(fresh, restored.username_digest(USER),
                              ("synthetic-revision", "synthetic-binding"), clock) as attempt:
            assert attempt.device_digest and not attempt.replayed


def test_completion_replay_is_audited_without_failing_password_sign_in(login_handler, monkeypatch):
    identity, login = login_handler
    original = _response_device(login())
    devices = identity._local_devices
    authenticate = identity.local_settings.authenticate.side_effect
    account = identity.local_settings.account(USER)
    revision = (account.session_revision, identity._local_session_binding(account))

    def race(username, password):
        with devices.attempt(original, devices.username_digest(USER), revision, identity._clock) as result:
            result.outcome, result.revision = "success", revision
        with devices.attempt(result.cookie, devices.username_digest(USER), revision, identity._clock):
            pass
        return authenticate(username, password)

    monkeypatch.setattr(identity.local_settings, "authenticate", race)
    response = login(original)
    assert response.status_code == 303 and not response.headers["location"].startswith("/auth/login?")
    cookies = SimpleCookie()
    for header in response.headers.getlist("set-cookie"):
        cookies.load(header)
    assert cookies[SESSION_COOKIE].value and DEVICE_COOKIE not in cookies
    assert all(row["state"] == "revoked" for row in _rows(identity))
    events = [event for event in identity.store.audit_events() if event.action == "auth.device.replay"]
    assert len(events) == 1 and events[0].outcome == "denied"


def test_same_cookie_successes_verify_concurrently_without_revoking(lane_identity, monkeypatch):
    identity, _, _ = lane_identity
    original = _sign_in(identity).device_cookie
    authenticate = identity.local_settings.authenticate.side_effect
    entered = threading.Barrier(2)

    def succeed(username, password):
        entered.wait(timeout=5)
        return authenticate(username, password)

    monkeypatch.setattr(identity.local_settings, "authenticate", succeed)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: _sign_in(identity, cookie=original), range(2)))
    assert len({result.device_cookie for result in results}) == 2
    assert all(result.device_cookie and not result.device_replayed for result in results)
    assert all(row["state"] != "revoked" for row in _rows(identity))
    assert sum(row["state"] == "active" for row in _rows(identity)) == 2


def test_wrong_password_on_stale_sibling_keeps_live_sibling(registry):
    devices, clock = registry
    original = _registry_success(registry)
    stale = _registry_success(registry, original)
    live = _registry_success(registry, original)
    digest = devices.username_digest(USER)
    revision = ("synthetic-revision", "synthetic-binding")
    with devices.attempt(stale, digest, revision, clock) as result:
        result.outcome = "failure"
    with devices._transaction() as db:
        row = devices._row(db, live)
        assert row is not None
        assert row["state"] == "active"
        assert all(row[0] != "revoked" for row in db.execute("SELECT state FROM devices"))
    with devices.attempt(live, digest, revision, clock) as result:
        assert result.device_digest is not None and not result.replayed
        result.outcome, result.revision = "success", revision
    assert result.cookie


def test_unused_sibling_is_deleted_when_another_signs_in(registry):
    devices, clock = registry
    original = _registry_success(registry)
    displaced = _registry_success(registry, original)
    current = _registry_success(registry, original)
    digest = devices.username_digest(USER)
    revision = ("synthetic-revision", "synthetic-binding")
    with devices.attempt(current, digest, revision, clock) as result:
        result.outcome = "failure"
    with devices._transaction() as db:
        assert devices._row(db, displaced)["state"] == "active"
    with devices.attempt(current, digest, revision, clock) as result:
        with devices._transaction() as db:
            assert devices._row(db, displaced)["state"] == "active"
        result.outcome, result.revision = "success", revision
    with devices.attempt(displaced, digest, revision, clock) as result:
        assert not result.replayed and result.device_digest is None
    with devices._transaction() as db:
        assert devices._row(db, displaced) is None
        assert devices._row(db, current)["state"] == "rotated"
        assert all(row[0] != "revoked" for row in db.execute("SELECT state FROM devices"))


def test_never_admitted_siblings_are_bounded_and_oldest_becomes_unknown(registry):
    devices, clock = registry
    original = _registry_success(registry)
    siblings = []
    assert devices.per_parent_unadmitted_limit == 5
    for index in range(12):
        siblings.append(_registry_success(registry, original))
        with devices._transaction() as db:
            parent = devices._row(db, original)["device_digest"]
            rows = db.execute("SELECT * FROM devices WHERE parent_digest = ?", (parent,)).fetchall()
            assert len(rows) == min(index + 1, 5)
            assert all(row["state"] == "active" and row["admitted"] == 0 for row in rows)
            assert all(devices._row(db, cookie) is not None for cookie in siblings[-5:])
            assert all(devices._row(db, cookie) is None for cookie in siblings[:-5])
    with devices.attempt(siblings[0], devices.username_digest(USER),
                         ("synthetic-revision", "synthetic-binding"), clock) as result:
        assert result.device_digest is None and not result.replayed
    with devices._transaction() as db:
        assert all(row[0] != "revoked" for row in db.execute("SELECT state FROM devices"))


def test_untrusted_rotation_marks_presented_predecessor_for_replay(registry):
    devices, clock = registry
    original = _registry_success(registry)
    digest = devices.username_digest(USER)
    revision = ("synthetic-new-revision", "synthetic-binding")
    with devices.attempt(original, digest, revision, clock) as result:
        assert result.device_digest is None
        result.outcome, result.revision = "success", revision
    with devices.attempt(result.cookie, digest, revision, clock) as successor:
        successor.outcome, successor.revision = "success", revision
    with devices.attempt(original, digest, revision, clock) as replay:
        assert replay.replayed and replay.device_digest is None
    with devices._transaction() as db:
        assert all(row[0] == "revoked" for row in db.execute("SELECT state FROM devices"))


def test_grace_lifetime_revocation_survives_pruning_predecessor(registry):
    devices, clock = registry
    original = _registry_success(registry)
    successor = _registry_success(registry, original)
    with devices._transaction() as db:
        # fixture-write: Seed the rotated device one failure below its lifetime limit without expiring rotation grace.
        db.execute("UPDATE devices SET failures = 29 WHERE state = 'rotated'")
    with devices.attempt(original, devices.username_digest(USER),
                         ("synthetic-revision", "synthetic-binding"), clock) as result:
        result.outcome = "failure"
    with devices._transaction() as db:
        assert all(row[0] == "revoked" for row in db.execute("SELECT state FROM devices"))
        # fixture-write: Remove only the exhausted predecessor to verify successor revocation survives pruning while the successor remains stored.
        db.execute("DELETE FROM devices WHERE failures = 30")
    with devices.attempt(successor, devices.username_digest(USER),
                         ("synthetic-revision", "synthetic-binding"), clock) as result:
        assert result.device_digest is None


def test_cap_keeps_chain_whose_newest_sibling_is_within_cap(registry):
    devices, clock = registry
    original = _registry_success(registry)
    first = _registry_success(registry, original)
    for _ in range(devices.per_user_active_limit - 2):
        clock.advance(seconds=1)
        _registry_success(registry)
    clock.advance(seconds=1)
    second = _registry_success(registry, original)
    clock.advance(seconds=1)
    _registry_success(registry)
    with devices._transaction() as db:
        row = devices._row(db, second)
        assert row["state"] == "active"
        assert devices._row(db, first)["state"] == "active"
        assert not db.execute("SELECT 1 FROM devices WHERE chain_id = ? AND state = 'revoked'",
                              (row["chain_id"],)).fetchone()


def test_backward_clock_step_does_not_reopen_grace(login_handler, monkeypatch):
    identity, login = login_handler
    elapsed = [0.0]
    monkeypatch.setattr("case_intelligence.local_login_devices.monotonic", lambda: elapsed[0])
    original = _response_device(login())
    _response_device(login(original))
    identity._clock.advance(seconds=90)
    elapsed[0] = 90
    _sign_in(identity, username=OTHER)
    identity._clock.advance(seconds=-80)
    results = []
    authenticate = identity.login_local

    def capture(*args, **kwargs):
        result = authenticate(*args, **kwargs)
        results.append(result)
        return result

    monkeypatch.setattr(identity, "login_local", capture)
    response = login(original)
    assert response.status_code == 303
    assert results[0].device_replayed
    chain = _rows(identity)[0]["chain_id"]
    assert all(row["state"] == "revoked" for row in _rows(identity) if row["chain_id"] == chain)
    events = [event for event in identity.store.audit_events() if event.action == "auth.device.replay"]
    assert len(events) == 1 and events[0].outcome == "denied"


@pytest.mark.parametrize("backward_step", [1.5, 2.5])
def test_backward_clock_step_preserves_live_lost_rotation(
        lane_identity, monkeypatch, backward_step):
    identity, clock, _ = lane_identity
    monkeypatch.setattr("case_intelligence.local_login_devices.monotonic", lambda: 100.0)
    original = _sign_in(identity).device_cookie
    successor = _sign_in(identity, cookie=original).device_cookie
    clock.advance(seconds=2)
    _sign_in(identity, username=OTHER)
    clock.advance(seconds=-backward_step)
    _lock_untrusted(identity)
    verifier = identity.local_settings.authenticate
    verifier.reset_mock()
    result = _sign_in(identity, cookie=original)
    assert result[1] and result.device_cookie and not result.device_replayed
    verifier.assert_called_once_with(USER, PASSWORD)
    devices = identity._local_devices
    with devices._transaction() as db:
        row = devices._row(db, successor)
        assert row["state"] == "active"
        assert not db.execute("SELECT 1 FROM devices WHERE chain_id = ? AND state = 'revoked'",
                              (row["chain_id"],)).fetchone()


def test_lifetime_failure_cap_holds_across_processes(registry):
    devices, clock = registry
    cookie = _registry_success(registry)
    second = LocalLoginDevices(devices.path.parent)
    with devices._transaction() as db:
        # fixture-write: Seed the lifetime budget with five remaining slots for independent registry owners.
        db.execute("UPDATE devices SET failures = 25")
    admitted, refused = [], 0
    with ExitStack() as attempts:
        for owner in (devices, second):
            for _ in range(5):
                try:
                    result = attempts.enter_context(owner.attempt(
                        cookie, owner.username_digest(USER),
                        ("synthetic-revision", "synthetic-binding"), clock))
                except LoginThrottled:
                    refused += 1
                else:
                    admitted.append(result)
                    result.outcome = "failure"
    with devices._transaction() as db:
        assert devices._row(db, cookie)["failures"] <= 30
    assert len(admitted) == 5 and refused == 5


def test_registry_error_falls_back_to_untrusted_lane(lane_identity, monkeypatch, caplog):
    identity, _, _ = lane_identity
    cookie = _sign_in(identity).device_cookie
    devices = identity._local_devices
    devices.path.write_bytes(b"synthetic corrupt registry")
    devices.path.chmod(0o600)
    verifier = identity.local_settings.authenticate
    verifier.reset_mock()
    untrusted = Mock(wraps=identity._local_limiter.attempt)
    monkeypatch.setattr(identity._local_limiter, "attempt", untrusted)
    result = _sign_in(identity, cookie=cookie)
    verifier.assert_called_once_with(USER, PASSWORD)
    untrusted.assert_called_once()
    assert result[1] and result.device_cookie is None and not result.device_replayed
    assert any(record.levelname == "WARNING" and "registry" in record.message.lower()
               for record in caplog.records)
    assert cookie not in caplog.text


def test_expired_pending_batch_is_counted_once_after_restart(registry):
    devices, clock = registry
    cookie = _registry_success(registry)
    digest = devices.username_digest(USER)
    revision = ("synthetic-revision", "synthetic-binding")
    with devices._transaction() as db:
        # fixture-write: Leave five lifetime slots to check abandoned reservations and late completion.
        db.execute("UPDATE devices SET failures = 25")
    with ExitStack() as pending:
        for _ in range(5):
            attempt = pending.enter_context(devices.attempt(cookie, digest, revision, clock))
            attempt.outcome = "failure"
        restarted = LocalLoginDevices(devices.path.parent)
        with pytest.raises(LoginThrottled):
            with restarted.attempt(cookie, digest, revision, clock):
                pytest.fail("opening another owner cleared live reservations")
        clock.advance(seconds=devices.pending_timeout_seconds + 1)
        with restarted.attempt(cookie, digest, revision, clock) as expired:
            assert expired.device_digest is None
        with restarted._transaction() as db:
            row = restarted._row(db, cookie)
            assert row["pending"] == 0 and row["failures"] == 30 and row["state"] == "revoked"
    with devices._transaction() as db:
        row = devices._row(db, cookie)
        assert row["pending"] == 0 and row["failures"] == 30


def test_pending_schema_migration_and_clean_restore(registry, tmp_path):
    devices, clock = registry
    cookie = _registry_success(registry)
    with devices._transaction() as db:
        for column in ("pending", "pending_until", "pending_generation", "rotation_tick", "rotation_epoch"):
            # fixture-write: Construct the previous registry schema for migration and backup restoration.
            db.execute(f"ALTER TABLE devices DROP COLUMN {column}")
        # fixture-write: Construct the previous registry without the persisted clock observation.
        db.execute("DROP TABLE device_clock")
    backup = tmp_path / "pending-schema-backup"
    shutil.copytree(devices.path.parent, backup)
    restored_path = tmp_path / "pending-schema-restored"
    shutil.copytree(backup, restored_path)
    for root in (devices.path.parent, restored_path):
        migrated = LocalLoginDevices(root)
        migrated = LocalLoginDevices(root)
        with migrated._transaction() as db:
            assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            row = migrated._row(db, cookie)
            assert row["pending"] == row["pending_until"] == row["pending_generation"] == 0
        assert _registry_success((migrated, clock), cookie)


@pytest.mark.parametrize("forward_step", [0, 3600])
@pytest.mark.parametrize("reopen", [False, True])
def test_monotonic_elapsed_time_closes_grace_without_intervening_requests(
        registry, monkeypatch, forward_step, reopen):
    devices, clock = registry
    elapsed = [0.0]
    monkeypatch.setattr("case_intelligence.local_login_devices.monotonic", lambda: elapsed[0])
    original = _registry_success(registry)
    clock.advance(seconds=forward_step)
    _registry_success(registry, original)
    elapsed[0] = 90
    clock.advance(seconds=10)
    if reopen:
        devices = LocalLoginDevices(devices.path.parent)
    with devices.attempt(original, devices.username_digest(USER),
                         ("synthetic-revision", "synthetic-binding"), clock) as attempt:
        assert attempt.replayed and attempt.device_digest is None


def test_active_entry_bound_counts_sibling_chains(registry):
    devices, _ = registry
    for _ in range(devices.per_user_active_limit + 1):
        original = _registry_success(registry)
        for _ in range(devices.per_parent_unadmitted_limit):
            _registry_success(registry, original)
        with devices._transaction() as db:
            rows = db.execute("SELECT chain_id FROM devices WHERE state = 'active'").fetchall()
            assert len({row["chain_id"] for row in rows}) <= devices.per_user_active_limit
            assert len(rows) <= devices.per_user_active_limit * devices.per_parent_unadmitted_limit


@pytest.mark.parametrize("timing", ["missing-epoch", "previous-boot", "missing-tick", "future-tick"])
def test_missing_or_previous_boot_rotation_timing_fails_closed(registry, monkeypatch, timing):
    devices, clock = registry
    monkeypatch.setattr("case_intelligence.local_login_devices.monotonic", lambda: 100.0)
    original = _registry_success(registry)
    current = _registry_success(registry, original)
    with devices._transaction() as db:
        # fixture-write: Represent legacy rotation timing or a restored registry from a previous boot.
        if timing in ("missing-epoch", "previous-boot"):
            epoch = None if timing == "missing-epoch" else "synthetic-previous-boot"
            # fixture-write: Represent legacy rotation timing or a restored registry from a previous boot.
            db.execute("UPDATE devices SET rotation_epoch = ? WHERE state = 'rotated'", (epoch,))
        else:
            tick = None if timing == "missing-tick" else 101.0
            # fixture-write: Represent legacy rotation timing or a restored registry from a previous boot.
            db.execute("UPDATE devices SET rotation_tick = ? WHERE state = 'rotated'", (tick,))
    with devices.attempt(original, devices.username_digest(USER),
                         ("synthetic-revision", "synthetic-binding"), clock) as attempt:
        assert attempt.replayed and attempt.device_digest is None
    with devices._transaction() as db:
        assert devices._row(db, current)["state"] == "revoked"
