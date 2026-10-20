"""Synthetic regressions for local login client trust and bounded guessing."""

import inspect
import json
import re
import socket
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock

import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient
from starlette.requests import Request

from case_intelligence import workbench
from case_intelligence.generation import UnavailableGenerator
from case_intelligence.identity import (
    IdentityService, LocalAccountSettings, LocalAuthenticationError,
    LOGIN_CHALLENGE_COOKIE, SESSION_COOKIE,
)
from case_intelligence.local_account_format import LocalAccount
from case_intelligence.workbench import create_workbench_app
from case_intelligence.workspace_store import WorkspaceStore

GATEWAY_PEER = ("192.0.2.10", 41000)  # what uvicorn sees for every proxied request
PASSWORD = "synthetic-correct-password"

def _accounts(tmp_path):
    path = tmp_path / "accounts.json"
    path.write_text(json.dumps({"format_version": 1, "accounts": [{
        "username": "synthetic.admin", "display_name": "Synthetic Admin",
        "password_hash": PasswordHasher().hash(PASSWORD),
        "roles": ["administrator"], "enabled": True}]}), encoding="utf-8")
    path.chmod(0o600)
    return path

def _challenge(client):
    page = client.get("/auth/login")
    return re.search(r'name="login_challenge" value="([^"]+)"', page.text).group(1)

def _login(client, password, xff):
    return client.post("/auth/local", headers={"X-Forwarded-For": xff}, data={
        "username": "synthetic.admin", "password": password,
        "login_challenge": _challenge(client), "next": "/"}, follow_redirects=False)

def test_remote_attacker_cannot_lock_out_another_person(tmp_path):
    app = create_workbench_app(
        tmp_path / "runtime", generator=UnavailableGenerator(), auth_mode="local",
        secure_cookie=True, local_settings=LocalAccountSettings(_accounts(tmp_path)),
        local_gateway_peers=(GATEWAY_PEER[0],),
        answer_workers=1)
    base = "https://exculpata.example.test"
    with TestClient(app, base_url=base, client=GATEWAY_PEER) as attacker, \
         TestClient(app, base_url=base, client=GATEWAY_PEER) as victim:
        for _ in range(10):                       # attacker at 203.0.113.66
            _login(attacker, "wrong-guess", "203.0.113.66")
        response = _login(victim, PASSWORD, "198.51.100.7")   # real admin, other machine
        assert "temporarily+unavailable" not in response.headers.get("location", ""), (
            "correct password from a different person was refused: "
            + response.headers.get("location", ""))
        assert victim.cookies.get(SESSION_COOKIE) is not None


def _app(tmp_path):
    return create_workbench_app(
        tmp_path / "runtime", generator=UnavailableGenerator(), auth_mode="local",
        secure_cookie=True, local_settings=LocalAccountSettings(_accounts(tmp_path)),
        local_gateway_peers=(GATEWAY_PEER[0],), answer_workers=1)


def _assert_denied(response, client):
    assert response.status_code == 303
    assert response.headers["location"].startswith("/auth/login?")
    assert client.cookies.get(SESSION_COOKIE) is None


def test_route_same_client_is_throttled_even_with_correct_password(tmp_path):
    with TestClient(_app(tmp_path), base_url="https://exculpata.example.test",
                    client=GATEWAY_PEER) as client:
        for _ in range(15):
            _assert_denied(_login(client, "wrong-guess", "203.0.113.66"), client)
        _assert_denied(_login(client, PASSWORD, "203.0.113.66"), client)


MALFORMED = ["192.0.2.1, 198.51.100.1", "192.0.2.1:80", "", "garbage", " 192.0.2.1",
             "192.0.2.1 ", "192.0.2.1\t", "[2001:db8::1]", "2001:db8::1%eth0"]


@pytest.mark.parametrize("peer,values", [
    (("192.0.2.11", 42000), [f"203.0.113.{i}" for i in range(1, 16)]),
    (GATEWAY_PEER, MALFORMED),
])
def test_route_forged_headers_cannot_rotate_client_key(tmp_path, peer, values):
    with TestClient(_app(tmp_path), base_url="https://exculpata.example.test",
                    client=peer) as client:
        for i in range(15):
            _assert_denied(_login(client, "wrong-guess", values[i % len(values)]), client)
        _assert_denied(_login(client, PASSWORD, values[0]), client)


def _request(peer=GATEWAY_PEER, values=("203.0.113.66",)):
    return Request({"type": "http", "client": peer,
                    "headers": [(b"x-forwarded-for", value.encode()) for value in values]})


def _key(peer=GATEWAY_PEER, values=("203.0.113.66",)):
    return workbench._local_login_client_key(_request(peer, values), (GATEWAY_PEER[0],))


@pytest.mark.parametrize("values", [(value,) for value in MALFORMED] + [(),
    ("192.0.2.1", "198.51.100.1"), ("192.0.2.1", "192.0.2.1")])
def test_unit_malformed_or_duplicate_headers_use_direct_peer(values):
    assert _key(values=values) == GATEWAY_PEER[0]


def test_unit_trusted_header_and_ipv6_canonicalization():
    assert _key() == "203.0.113.66"
    assert _key(values=("2001:0db8:0:0:0:0:0:1",)) == "2001:db8::1"
    assert _key(peer=None) == "unknown"
    assert _key(peer=("192.0.2.9", 43000)) == "192.0.2.9"


def test_ipv4_mapped_trusted_peer_uses_forwarded_client():
    assert _key(peer=("::ffff:192.0.2.10", 41000), values=("203.0.113.9",)) == "203.0.113.9"
    request = _request(values=("203.0.113.9",))
    assert workbench._local_login_client_key(request, ("::ffff:192.0.2.10",)) == "203.0.113.9"


@pytest.mark.parametrize("installed", [False, True])
def test_unit_compose_gateway_resolution(monkeypatch, installed):
    monkeypatch.setenv("EXCULPATA_LOCAL_LOGIN_TRUST_GATEWAY", "1" if installed else "0")
    resolve = Mock(return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", GATEWAY_PEER)])
    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    expected = "203.0.113.66" if installed else GATEWAY_PEER[0]
    assert workbench._local_login_client_key(_request()) == expected
    assert resolve.call_count == int(installed)
    if installed:
        assert resolve.call_args.args[:2] == ("gateway", None)
    assert workbench._local_login_client_key(_request(), ()) == GATEWAY_PEER[0]
    resolve.side_effect = socket.gaierror("synthetic resolution failure")
    assert workbench._local_login_client_key(_request()) == GATEWAY_PEER[0]


def test_unit_gateway_address_changes_do_not_retain_old_trust(monkeypatch):
    monkeypatch.setenv("EXCULPATA_LOCAL_LOGIN_TRUST_GATEWAY", "1")
    resolve = Mock(side_effect=[
        [(socket.AF_INET, socket.SOCK_STREAM, 6, "", GATEWAY_PEER)],
        [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.20", 0))]])
    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    assert workbench._local_login_client_key(_request()) == "203.0.113.66"
    assert workbench._local_login_client_key(_request()) == GATEWAY_PEER[0]


@pytest.mark.parametrize("peers", [("192.0.2.0/24",), ("garbage",),
                                   (GATEWAY_PEER[0], "garbage")])
def test_unit_invalid_gateway_configuration_ignores_header(peers):
    assert workbench._local_login_client_key(_request(), peers) == GATEWAY_PEER[0]


def test_gateway_overwrites_browser_forwarded_prefix_in_both_locations():
    template = (Path(__file__).resolve().parents[1] / "deploy/gateway/default.conf.template").read_text()
    directives = re.findall(r"proxy_set_header X-Forwarded-For ([^;]+);", template)
    assert directives == ["$remote_addr", "$remote_addr"]


@pytest.fixture
def identity(tmp_path):
    account = LocalAccount("synthetic.admin", "Synthetic Admin", "synthetic-hash",
                           frozenset({"administrator"}))
    settings = Mock(spec=LocalAccountSettings, provider_key="local")
    settings.authenticate.side_effect = lambda username, password: (
        account if username.strip().casefold() == account.username and password == PASSWORD else None)
    store = WorkspaceStore(tmp_path / "runtime/workbench.sqlite")
    service = IdentityService(store, tmp_path / "runtime", auth_mode="local",
                              secure_cookie=True, local_settings=settings)
    service._secret = bytes(range(32))
    try:
        yield service
    finally:
        store.close()


def _wrong(identity, client, username="synthetic.admin"):
    with pytest.raises(LocalAuthenticationError):
        identity.login_local(username, "wrong-guess", client_key=client)


def test_unit_remote_attacker_cannot_lock_out_another_person(identity):
    for _ in range(10):
        _wrong(identity, _key())
    _, token = identity.login_local("synthetic.admin", PASSWORD,
                                    client_key=_key(values=("198.51.100.7",)))
    assert token


def test_unit_login_handler_sets_cookie_only_for_other_client(tmp_path, identity):
    app = create_workbench_app(
        tmp_path / "handler-runtime", generator=UnavailableGenerator(), auth_mode="local",
        secure_cookie=True, local_settings=identity.local_settings,
        local_gateway_peers=(GATEWAY_PEER[0],), answer_workers=1)
    endpoint = next(route.endpoint for route in app.routes
                    if getattr(route, "path", None) == "/auth/local")

    def login(password, address):
        challenge = app.state.identity.new_login_challenge()
        request = _request(values=(address,))
        request.scope["headers"].append(
            (b"cookie", f"{LOGIN_CHALLENGE_COOKIE}={challenge}".encode()))
        return endpoint(request, username="synthetic.admin", password=password,
                        login_challenge=challenge, next_path="/")

    try:
        for _ in range(15):
            response = login("wrong-guess", "203.0.113.66")
            assert response.status_code == 303
            assert response.headers["location"].startswith("/auth/login?")
        blocked = login(PASSWORD, "203.0.113.66")
        assert blocked.status_code == 303
        assert blocked.headers["location"].startswith("/auth/login?")
        assert SESSION_COOKIE not in blocked.headers.get("set-cookie", "")
        accepted = login(PASSWORD, "198.51.100.7")
        assert accepted.status_code == 303
        assert not accepted.headers["location"].startswith("/auth/login")
        assert SESSION_COOKIE in accepted.headers.get("set-cookie", "")
    finally:
        app.state.workbench.close()


@pytest.mark.parametrize("peer,values", [
    (("192.0.2.11", 42000), [f"203.0.113.{i}" for i in range(1, 16)]),
    (GATEWAY_PEER, MALFORMED),
])
def test_unit_forged_headers_cannot_rotate_client_key(identity, monkeypatch, peer, values):
    authenticate = Mock(return_value=None)
    monkeypatch.setattr(identity.local_settings, "authenticate", authenticate)
    for i in range(15):
        _wrong(identity, _key(peer, (values[i % len(values)],)))
    assert authenticate.call_count == 10
    with pytest.raises(LocalAuthenticationError):
        identity.login_local("synthetic.admin", PASSWORD, client_key=_key(peer, (values[0],)))
    assert authenticate.call_count == 10


def test_unit_aggregate_bound_across_clients(identity, monkeypatch):
    authenticate = Mock(return_value=None)
    monkeypatch.setattr(identity.local_settings, "authenticate", authenticate)
    for i in range(110):
        _wrong(identity, f"203.0.113.{i + 1}", " SYNTHETIC.ADMIN " if i % 2 else "synthetic.admin")
    assert authenticate.call_count == 100
    with pytest.raises(LocalAuthenticationError):
        identity.login_local("synthetic.admin", PASSWORD, client_key="198.51.100.7")
    assert authenticate.call_count == 100
    _wrong(identity, "198.51.100.7", "synthetic.other")
    assert authenticate.call_count == 101


def test_unit_success_clears_only_same_client_username_failures(identity):
    for _ in range(9):
        _wrong(identity, "203.0.113.66")
    _wrong(identity, "203.0.113.66", "synthetic.other")
    for _ in range(10):
        _wrong(identity, "198.51.100.200")
    assert identity.login_local("synthetic.admin", PASSWORD, client_key="203.0.113.66")[1]
    for _ in range(9):
        _wrong(identity, "203.0.113.66")
    assert identity.login_local("synthetic.admin", PASSWORD, client_key="203.0.113.66")[1]
    with pytest.raises(LocalAuthenticationError, match="^Sign-in is temporarily unavailable"):
        identity.login_local("synthetic.admin", PASSWORD, client_key="198.51.100.200")
    client = identity._local_limiter.clients[identity._local_attempt_key("", "203.0.113.66")]
    other_key = identity._local_attempt_key("synthetic.other", "")
    assert client.usernames[other_key].count == 1
    for i in range(72):
        _wrong(identity, f"198.51.100.{i + 1}")
    with pytest.raises(LocalAuthenticationError, match="^Sign-in is temporarily unavailable"):
        identity.login_local("synthetic.admin", PASSWORD, client_key="192.0.2.1")


def test_unit_aggregate_refusals_do_not_extend_window(identity, monkeypatch):
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    monkeypatch.setattr(identity, "_clock", lambda: now)
    authenticate = Mock(return_value=None)
    monkeypatch.setattr(identity.local_settings, "authenticate", authenticate)
    for i in range(100):
        _wrong(identity, f"203.0.113.{i + 1}")
    now += timedelta(minutes=14)
    _wrong(identity, "198.51.100.7")
    assert authenticate.call_count == 100
    now += timedelta(minutes=1, seconds=1)
    _wrong(identity, "198.51.100.7")
    assert authenticate.call_count == 101


def test_unit_client_cap_below_aggregate_and_refusals_do_not_extend_window(identity, monkeypatch):
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    monkeypatch.setattr(identity, "_clock", lambda: now)
    authenticate = Mock(return_value=None)
    monkeypatch.setattr(identity.local_settings, "authenticate", authenticate)
    for _ in range(150):
        _wrong(identity, "203.0.113.66")
    assert authenticate.call_count == 10
    _wrong(identity, "198.51.100.7")
    assert authenticate.call_count == 11
    now += timedelta(minutes=14)
    _wrong(identity, "203.0.113.66")
    assert authenticate.call_count == 11
    now += timedelta(minutes=1, seconds=1)
    _wrong(identity, "203.0.113.66")
    assert authenticate.call_count == 12


@pytest.mark.parametrize("distinct_clients,expected", [(False, 10), (True, 100)])
def test_unit_concurrent_attempts_respect_both_bounds(identity, monkeypatch, distinct_clients, expected):
    barrier = threading.Barrier(12)

    def fail(username, password):
        # Release the GIL while a verification is in progress, as Argon2 does.
        threading.Event().wait(0.002)
        return None

    authenticate = Mock(side_effect=fail)
    monkeypatch.setattr(identity.local_settings, "authenticate", authenticate)

    def attack(worker):
        barrier.wait(timeout=10)
        for attempt in range(10):
            client = f"203.0.113.{worker * 10 + attempt + 1}" if distinct_clients else "203.0.113.66"
            _wrong(identity, client)

    with ThreadPoolExecutor(max_workers=12) as pool:
        list(pool.map(attack, range(12)))
    assert authenticate.call_count == expected


def test_unit_username_churn_cannot_refuse_other_clients_correct_password(identity, monkeypatch):
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    monkeypatch.setattr(identity, "_clock", lambda: now)
    for i in range(10_010):
        _wrong(identity, "203.0.113.66", f"synthetic.user{i}")
    assert identity.login_local("synthetic.admin", PASSWORD, client_key="198.51.100.7")[1]


def test_unit_username_churn_has_bounded_memory_and_preserves_own_throttle(identity, monkeypatch):
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    monkeypatch.setattr(identity, "_clock", lambda: now)
    for _ in range(10):
        _wrong(identity, "203.0.113.66")
    for i in range(10_010):
        _wrong(identity, "203.0.113.66", f"synthetic.user{i}")
    assert identity.local_settings.authenticate.call_count == 50
    limiter = identity._local_limiter
    assert len(limiter.clients) == 1
    client = next(iter(limiter.clients.values()))
    assert len(client.usernames) <= 50
    assert len(client.total.failures) == 50
    assert len(limiter.usernames) <= 50
    with pytest.raises(LocalAuthenticationError, match="^Sign-in is temporarily unavailable"):
        identity.login_local("synthetic.admin", PASSWORD, client_key="203.0.113.66")
    now += timedelta(minutes=15, seconds=1)
    assert identity.login_local("synthetic.admin", PASSWORD, client_key="203.0.113.66")[1]


def test_unit_different_clients_verify_concurrently(identity, monkeypatch):
    entered = threading.Barrier(2)
    overlapped = []

    def fail(username, password):
        try:
            entered.wait(timeout=2)
            overlapped.append(True)
        except threading.BrokenBarrierError:
            pass
        return None

    monkeypatch.setattr(identity.local_settings, "authenticate", fail)
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda client: _wrong(identity, client), ["192.0.2.1", "192.0.2.2"]))
    assert len(overlapped) == 2


def test_unit_verifier_error_releases_lock_without_counting_failure(identity, monkeypatch):
    authenticate = Mock(side_effect=RuntimeError("synthetic unavailable"))
    monkeypatch.setattr(identity.local_settings, "authenticate", authenticate)
    for _ in range(12):
        with pytest.raises(LocalAuthenticationError, match="^Local account sign-in is temporarily unavailable\\.$"):
            identity.login_local("synthetic.admin", PASSWORD, client_key="203.0.113.66")
    assert authenticate.call_count == 12
    assert not identity._local_limiter.clients
    assert not identity._local_limiter.usernames


def test_unit_full_client_table_admits_fresh_client_and_retains_aggregate(identity):
    limiter = identity._local_limiter
    limiter.client_capacity = 2
    for client in ("192.0.2.1", "192.0.2.2", "192.0.2.3"):
        for _ in range(10):
            _wrong(identity, client)
        assert len(limiter.clients) <= 2
    assert identity.login_local("synthetic.admin", PASSWORD, client_key="198.51.100.7")[1]
    for i in range(70):
        _wrong(identity, f"203.0.113.{i + 1}")
    with pytest.raises(LocalAuthenticationError, match="^Sign-in is temporarily unavailable"):
        identity.login_local("synthetic.admin", PASSWORD, client_key="198.51.100.7")
    assert len(limiter.clients) <= 2


def test_unit_colliding_usernames_remain_bounded_without_single_client_lockout(identity):
    limiter = identity._local_limiter
    limiter.username_bucket_count = 1
    for i in range(110):
        _wrong(identity, "203.0.113.66", f"synthetic.user{i}")
    assert identity.local_settings.authenticate.call_count == 50
    assert identity.login_local("synthetic.admin", PASSWORD, client_key="198.51.100.7")[1]
    for i in range(100):
        _wrong(identity, "192.0.2.2", f"synthetic.other{i}")
    assert len(limiter.usernames) == 1
    assert len(next(iter(limiter.usernames.values())).failures) == 100
    with pytest.raises(LocalAuthenticationError, match="^Sign-in is temporarily unavailable"):
        identity.login_local("synthetic.admin", PASSWORD, client_key="198.51.100.7")


@pytest.mark.parametrize("successful", [False, True])
def test_unit_pending_reservations_survive_expiry_and_success(identity, monkeypatch, successful):
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    monkeypatch.setattr(identity, "_clock", lambda: now)
    entered = threading.Event()
    release = threading.Event()
    original = identity.local_settings.authenticate.side_effect

    def slow(username, password):
        entered.set()
        assert release.wait(timeout=5)
        return original(username, password)

    for _ in range(8):
        _wrong(identity, "192.0.2.1")
    monkeypatch.setattr(identity.local_settings, "authenticate", slow)
    with ThreadPoolExecutor(max_workers=2) as pool:
        pending = pool.submit(_wrong, identity, "192.0.2.1")
        assert entered.wait(timeout=5)
        monkeypatch.setattr(identity.local_settings, "authenticate", original)
        try:
            if successful:
                assert identity.login_local("synthetic.admin", PASSWORD, client_key="192.0.2.1")[1]
            else:
                now += timedelta(minutes=16)
            for _ in range(9):
                _wrong(identity, "192.0.2.1")
            with pytest.raises(LocalAuthenticationError, match="^Sign-in is temporarily unavailable"):
                identity.login_local("synthetic.admin", PASSWORD, client_key="192.0.2.1")
        finally:
            release.set()
        pending.result(timeout=5)
    assert identity._local_limiter.clients
    assert all(client.total.pending == 0 for client in identity._local_limiter.clients.values())


def test_unit_local_login_route_keeps_gateway_lookup_off_event_loop(tmp_path, identity):
    app = create_workbench_app(
        tmp_path / "worker-runtime", generator=UnavailableGenerator(), auth_mode="local",
        secure_cookie=True, local_settings=identity.local_settings, answer_workers=1)
    try:
        endpoint = next(route.endpoint for route in app.routes
                        if getattr(route, "path", None) == "/auth/local")
        assert not inspect.iscoroutinefunction(endpoint)
    finally:
        app.state.workbench.close()


def test_unit_concurrent_username_churn_reserves_client_total(identity, monkeypatch):
    barrier = threading.Barrier(12)

    def fail(username, password):
        threading.Event().wait(0.002)
        return None

    authenticate = Mock(side_effect=fail)
    monkeypatch.setattr(identity.local_settings, "authenticate", authenticate)

    def attack(worker):
        barrier.wait(timeout=5)
        for attempt in range(10):
            _wrong(identity, "192.0.2.1", f"synthetic.user{worker}_{attempt}")

    with ThreadPoolExecutor(max_workers=12) as pool:
        list(pool.map(attack, range(12)))
    assert authenticate.call_count == 50
    assert len(identity._local_limiter.clients) == 1


def test_unit_full_table_waits_for_pending_verification_then_admits_fresh_client(identity, monkeypatch):
    limiter = identity._local_limiter
    limiter.client_capacity = 1
    entered = threading.Event()
    release = threading.Event()
    waiting = threading.Event()
    original = identity.local_settings.authenticate.side_effect
    original_wait = limiter._condition.wait

    def wait():
        waiting.set()
        return original_wait(timeout=5)

    def slow(username, password):
        if password != PASSWORD:
            entered.set()
            assert release.wait(timeout=5)
        return original(username, password)

    monkeypatch.setattr(limiter._condition, "wait", wait)
    monkeypatch.setattr(identity.local_settings, "authenticate", slow)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(_wrong, identity, "192.0.2.1")
        try:
            assert entered.wait(timeout=5)
            second = pool.submit(identity.login_local, "synthetic.admin", PASSWORD,
                                 client_key="192.0.2.2")
            assert waiting.wait(timeout=5)
            assert len(limiter.clients) == 1
            assert not second.done()
        finally:
            release.set()
        first.result(timeout=5)
        assert second.result(timeout=5)[1]
    assert len(limiter.clients) <= 1
    assert all(window.pending == 0 for window in limiter.usernames.values())


def test_unit_global_state_bounded_under_distinct_clients_and_usernames(identity):
    limiter = identity._local_limiter
    limiter.client_capacity = 3
    limiter.username_bucket_count = 7
    for i in range(1_000):
        _wrong(identity, f"2001:db8::{i + 1:x}", f"synthetic.user{i}")
        assert len(limiter.clients) <= 3
        assert len(limiter.usernames) <= 7
        assert all(len(client.usernames) <= 50 and client.total.count <= 50
                   for client in limiter.clients.values())
        assert all(window.count <= 100 for window in limiter.usernames.values())


@pytest.mark.parametrize("exception", [OSError, RuntimeError, ValueError])
def test_unit_verifier_exception_always_releases_pending_reservations(identity, monkeypatch, exception):
    monkeypatch.setattr(identity.local_settings, "authenticate",
                        Mock(side_effect=exception("synthetic unavailable")))
    expected = ValueError if exception is ValueError else LocalAuthenticationError
    with pytest.raises(expected):
        identity.login_local("synthetic.admin", PASSWORD, client_key="192.0.2.1")
    assert not identity._local_limiter.clients
    assert not identity._local_limiter.usernames
