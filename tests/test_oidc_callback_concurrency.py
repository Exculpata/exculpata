"""Overlapping callback cleanup must not take another callback's step-up request."""
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient

from case_intelligence.identity import OIDC_STATE_COOKIE, SESSION_COOKIE
from tests.test_case_oidc import oidc_case, post, context, MODE


@pytest.mark.parametrize('fresh_claims', [True, False])
def test_duplicate_callback_preserves_claimed_stepup(oidc_case, monkeypatch, fresh_claims):
    client, bench, matter, provider, _, _, _ = oidc_case
    identity = client.app.state.identity
    workspace = bench.workspace
    old_context = context(client)
    old_token = client.cookies.get(SESSION_COOKIE)
    assert post(client, matter, 'close/begin', mode=MODE).status_code == 302
    state = client.cookies.get(OIDC_STATE_COOKIE)
    digest = identity.token_digest(state)
    if not fresh_claims:
        provider.overrides['auth_time'] = None
    calls_before = len(provider.authentication_calls)
    consumed, resume = Event(), Event()
    original = workspace.consume_oidc_transaction

    def pause_after_consumption(state_digest, *, now):
        destination = original(state_digest, now=now)
        if state_digest == digest and destination is not None:
            consumed.set()
            assert resume.wait(10), 'synthetic callback interleaving timed out'
        return destination

    monkeypatch.setattr(workspace, 'consume_oidc_transaction', pause_after_consumption)
    duplicate = TestClient(client.app, base_url=identity.oidc_settings.external_origin,
                           cookies={SESSION_COOKIE: old_token})
    params = {'state': state, 'code': 'synthetic-stepup-code'}
    with ThreadPoolExecutor(max_workers=1) as callbacks:
        winner = callbacks.submit(client.get, '/auth/oidc/callback', params=params,
                                  follow_redirects=False)
        try:
            assert consumed.wait(10), 'winning callback did not consume its transaction'
            for _ in range(2):
                duplicate.cookies.set(OIDC_STATE_COOKIE, state, path='/auth/oidc',
                    domain=urlsplit(identity.oidc_settings.external_origin).hostname)
                denied = duplicate.get('/auth/oidc/callback', params=params, follow_redirects=False)
                assert denied.status_code == 303 and 'error=oidc_expired' in denied.headers['location']
                assert duplicate.cookies.get(SESSION_COOKIE) == old_token
                assert duplicate.cookies.get(OIDC_STATE_COOKIE) is None
                assert workspace.connection.execute(
                    'SELECT count(*) FROM workbench_case_oidc_request WHERE state_digest=?',
                    (digest,)).fetchone()[0] == 1
                assert workspace.connection.execute(
                    'SELECT count(*) FROM workbench_case_oidc_authorization').fetchone()[0] == 0
            assert len(provider.authentication_calls) == calls_before
        finally:
            resume.set()
            duplicate.close()
        accepted = winner.result(timeout=10)

    assert len(provider.authentication_calls) == calls_before + 1
    assert accepted.status_code == 303
    assert client.cookies.get(OIDC_STATE_COOKIE) is None
    assert workspace.connection.execute(
        'SELECT count(*) FROM workbench_case_oidc_request WHERE state_digest=?', (digest,)).fetchone()[0] == 0
    assert workspace.matter_lifecycle(matter.matter_id).state == 'active'
    grants = workspace.connection.execute('SELECT * FROM workbench_case_oidc_authorization').fetchall()
    if fresh_claims:
        assert accepted.headers['location'] == f'/matters/{matter.slug}/close'
        assert len(grants) == 1
        assert context(client).session.session_id != old_context.session.session_id
        assert workspace.session_by_id(old_context.session.session_id).revoked_at is not None
        assert post(client, matter, 'close/begin', mode=MODE).status_code == 303
        assert workspace.case_lifecycle.current_exit(matter.matter_id).mode == MODE
    else:
        assert 'error=oidc_fresh' in accepted.headers['location']
        assert not grants and client.cookies.get(SESSION_COOKIE) == old_token
        assert workspace.case_lifecycle.current_exit(matter.matter_id) is None
