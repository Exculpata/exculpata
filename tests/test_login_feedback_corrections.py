"""Followed-redirect regressions for PR #343's three reviewed feedback gaps."""
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from case_intelligence.generation import UnavailableGenerator
from case_intelligence.identity import OIDC_STATE_COOKIE, SESSION_COOKIE
from case_intelligence.workbench import create_workbench_app
from tests.test_browser_local_accounts import configured_app, create, edit, login, ORIGIN
from tests.test_case_oidc import oidc_case, post, callback, context, MODE
from tests.test_oidc_authentication import _settings, FakeOidcProvider, _begin


@pytest.fixture
def oidc_app(tmp_path):
    settings = _settings()
    provider = FakeOidcProvider(settings)
    return create_workbench_app(tmp_path / 'runtime', generator=UnavailableGenerator(), auth_mode='oidc',
        secure_cookie=True, oidc_settings=settings, oidc_client=provider, answer_workers=1)


@pytest.mark.parametrize('field,value', [
    ('state', 'SYNTHETIC-STATE-' + 'x' * 257),
    ('code', 'SYNTHETIC-CODE-' + 'x' * 4097),
    ('state', 'SYNTHETIC-STATE-' + '\u00e9'),
    ('state', ''), ('code', ''), ('state', None), ('code', None),
    ('state', ['synthetic-first', 'synthetic-second']),
    ('code', ['synthetic-first', 'synthetic-second']),
])
def test_malformed_callback_never_reflects_and_cleans_browser_state(oidc_app, field, value):
    app = oidc_app
    with TestClient(app, base_url=app.state.identity.oidc_settings.external_origin) as client:
        state = _begin(client)
        params = [('state', state), ('code', 'synthetic-code')]
        params = [(name, item) for name, item in params if name != field]
        params += [(field, item) for item in (value if isinstance(value, list) else [value]) if item is not None]
        params.append(('next', 'https://synthetic-attacker.example/'))
        response = client.get('/auth/oidc/callback', params=params, follow_redirects=False)
        assert response.status_code == 303
        assert client.cookies.get(OIDC_STATE_COOKIE) is None
        assert client.cookies.get(SESSION_COOKIE) is None
        page = client.get(response.headers['location'])
        assert page.status_code == 200 and 'role="alert"' in page.text
        for marker in ('SYNTHETIC-', 'synthetic-first', 'synthetic-second', 'xxxxxxxx', 'synthetic-attacker', state):
            assert marker not in response.text + str(response.headers) + page.text
        digest = app.state.identity.token_digest(state)
        db = app.state.workbench.workspace.connection
        assert db.execute('SELECT count(*) FROM workbench_oidc_transaction WHERE state_digest=?', (digest,)).fetchone()[0] == 0
        assert not app.state.identity.oidc_client.authentication_calls
        client.cookies.set(OIDC_STATE_COOKIE, state, path='/auth/oidc')
        replay = client.get('/auth/oidc/callback', params={'state': state, 'code': 'synthetic-code'}, follow_redirects=False)
        assert 'error=oidc_expired' in replay.headers['location']
        assert client.cookies.get(SESSION_COOKIE) is None


def test_mismatched_callback_cancels_only_cookie_owned_transaction(oidc_app):
    app = oidc_app
    origin = app.state.identity.oidc_settings.external_origin
    with TestClient(app, base_url=origin) as first, TestClient(app, base_url=origin) as second:
        first_state, second_state = _begin(first), _begin(second)
        denied = first.get('/auth/oidc/callback', params={'state': second_state, 'code': 'synthetic-code'}, follow_redirects=False)
        assert 'error=oidc_expired' in denied.headers['location']
        assert first.cookies.get(SESSION_COOKIE) is None
        db = app.state.workbench.workspace.connection
        for state, count in ((first_state, 0), (second_state, 1)):
            assert db.execute('SELECT count(*) FROM workbench_oidc_transaction WHERE state_digest=?',
                (app.state.identity.token_digest(state),)).fetchone()[0] == count
        accepted = second.get('/auth/oidc/callback', params={'state': second_state, 'code': 'synthetic-code'}, follow_redirects=False)
        assert accepted.status_code == 303 and second.cookies.get(SESSION_COOKIE)


@pytest.mark.parametrize('failure', ['claims', 'expired', 'oversized_state', 'oversized_code', 'provider_error'])
def test_failed_stepup_shows_fixed_message_preserves_session_and_return_path(oidc_case, failure):
    from datetime import timedelta
    client, bench, matter, provider, clock, _, _ = oidc_case
    old_token = client.cookies.get(SESSION_COOKIE)
    assert post(client, matter, 'close/begin', mode=MODE).status_code == 302
    state = client.cookies.get(OIDC_STATE_COOKIE)
    values = {'state': state, 'code': 'synthetic-stepup-code'}
    if failure == 'claims':
        provider.overrides['auth_time'] = None
    elif failure == 'expired':
        clock[0] += timedelta(minutes=6)
    elif failure == 'oversized_state':
        values['state'] = 'SYNTHETIC-OVERSIZED-' + 'x' * 257
    elif failure == 'oversized_code':
        values['code'] = 'SYNTHETIC-OVERSIZED-' + 'x' * 4097
    else:
        values['error'] = 'SYNTHETIC-PROVIDER-ERROR'
    response = client.get('/auth/oidc/callback', params=values, follow_redirects=False)
    assert response.status_code == 303
    destination = response.headers['location']
    assert parse_qs(urlsplit(destination).query)['next'] == [f'/matters/{matter.slug}/close']
    page = client.get(destination)
    assert page.status_code == 200 and 'role="alert"' in page.text
    assert ('Fresh organization sign-in could not authorize this closure.' in page.text
            if failure == 'claims' else 'Organization sign-in' in page.text)
    assert f'href="/matters/{matter.slug}/close"' in page.text
    assert 'Return to the requested page' in page.text
    assert 'SYNTHETIC-' not in page.text + response.text + str(response.headers)
    assert client.cookies.get(SESSION_COOKIE) == old_token and context(client).is_administrator
    assert client.cookies.get(OIDC_STATE_COOKIE) is None
    assert bench.workspace.matter_lifecycle(matter.matter_id).state == 'active'
    assert bench.workspace.connection.execute('SELECT count(*) FROM workbench_case_oidc_authorization').fetchone()[0] == 0
    assert bench.workspace.connection.execute('SELECT count(*) FROM workbench_case_oidc_request').fetchone()[0] == 0
    assert bench.workspace.connection.execute('SELECT count(*) FROM workbench_oidc_transaction WHERE state_digest=?',
        (client.app.state.identity.token_digest(state),)).fetchone()[0] == 0
    # Returning to the workflow does not authorize closure; another fresh sign-in is required.
    assert post(client, matter, 'close/begin', mode=MODE).status_code == 302
    assert bench.workspace.matter_lifecycle(matter.matter_id).state == 'active'


@pytest.mark.parametrize('action,fields', [
    ('password', {'password': 'synthetic-new-password', 'password_confirm': 'synthetic-new-password'}),
    ('state', {'enabled': 'no', 'confirm_self': 'yes'}),
    ('role', {'role': 'reviewer', 'confirm_self': 'yes'}),
])
@pytest.mark.parametrize('audit_failure', [False, True])
def test_self_account_change_retains_fixed_followed_notice(tmp_path, monkeypatch, action, fields, audit_failure):
    app, _ = configured_app(tmp_path)
    with TestClient(app, base_url=ORIGIN) as client:
        login(client)
        assert create(client, 'second.admin', role='administrator').status_code == 303
        if audit_failure:
            original = app.state.workbench.workspace.append_audit_event
            def fail_completion(**event):
                if event['action'].endswith('.completed'):
                    raise OSError('synthetic unavailable audit completion')
                return original(**event)
            monkeypatch.setattr(app.state.workbench.workspace, 'append_audit_event', fail_completion)
        response = edit(client, 'alice.admin', action, **fields)
        assert response.status_code == 303
        code = 'account_changed_audit' if audit_failure else 'account_changed'
        assert parse_qs(urlsplit(response.headers['location']).query)['error'] == [code]
        assert client.cookies.get(SESSION_COOKIE) is None
        page = client.get(response.headers['location'])
        assert page.status_code == 200
        assert 'Your account changed. Sign in again to continue.' in page.text
        assert ('Audit completion needs operator attention; tell the installation operator.' in page.text) == audit_failure
        assert client.get('/admin/people', follow_redirects=False).status_code == 303


@pytest.mark.parametrize('cookie', [None, 'invalid.synthetic.state'])
def test_missing_or_malformed_cookie_cannot_cancel_returned_state(oidc_app, cookie):
    app = oidc_app
    origin = app.state.identity.oidc_settings.external_origin
    with TestClient(app, base_url=origin) as first, TestClient(app, base_url=origin) as second:
        state = _begin(second)
        if cookie:
            first.cookies.set(OIDC_STATE_COOKIE, cookie, path='/auth/oidc')
        denied = first.get('/auth/oidc/callback', params={'state': state, 'code': 'synthetic-code'}, follow_redirects=False)
        assert denied.status_code == 303 and 'error=oidc_expired' in denied.headers['location']
        assert first.cookies.get(SESSION_COOKIE) is None
        accepted = second.get('/auth/oidc/callback', params={'state': state, 'code': 'synthetic-code'}, follow_redirects=False)
        assert accepted.status_code == 303 and second.cookies.get(SESSION_COOKIE)
