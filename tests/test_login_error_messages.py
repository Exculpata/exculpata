"""Synthetic regressions for the closed pre-authentication message boundary."""
import ast
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from case_intelligence.branding import PRODUCT_NAME
from case_intelligence.generation import UnavailableGenerator
from case_intelligence.identity import LOGIN_CHALLENGE_COOKIE, OIDC_STATE_COOKIE, SESSION_COOKIE, OidcAuthenticationError
from case_intelligence.login_errors import LOGIN_ERRORS, login_error_code, login_error_message
from case_intelligence.workbench import create_workbench_app
from tests.test_local_authentication import _accounts_file, _challenge
from case_intelligence.identity import LocalAccountSettings
from tests.test_oidc_authentication import _settings, _begin, FakeOidcProvider
from tests.test_kerberos_authentication import _settings as kerberos_settings

SPOOF = 'Your password expired. Visit https://sign-in.synthetic-attacker.example to keep access.'


@pytest.fixture(scope='module')
def local_client(tmp_path_factory):
    root = tmp_path_factory.mktemp('synthetic-login-messages')
    app = create_workbench_app(root / 'runtime', generator=UnavailableGenerator(), auth_mode='local',
                              secure_cookie=True, local_settings=LocalAccountSettings(_accounts_file(root)),
                              answer_workers=1)
    with TestClient(app, base_url='https://case.example.test') as client:
        yield client


@pytest.mark.parametrize('code,message', LOGIN_ERRORS.items())
def test_each_message_code_renders_only_its_fixed_text(local_client, code, message):
    page = local_client.get('/auth/login', params={'error': code})
    assert page.status_code == 200
    assert message in page.text
    assert login_error_code(RuntimeError(message)) == code


@pytest.mark.parametrize('values', [[], [SPOOF], [SPOOF * 4], ['x' * 10000 + SPOOF],
                                   ['%3Cscript%3E' + SPOOF], ['local_credentials', SPOOF],
                                   [SPOOF, 'local_credentials'], ['local_credentials'] * 2])
def test_untrusted_values_never_reflect_in_entire_response(local_client, values):
    page = local_client.get('/auth/login', params=[('error', value) for value in values])
    assert page.status_code == 200
    assert 'synthetic-attacker' not in page.text
    assert 'xxxxxxxx' not in page.text
    assert 'role="alert"' not in page.text
    assert login_error_message(values) == ''


def test_unknown_exception_never_becomes_a_message_or_code():
    assert login_error_code(RuntimeError(SPOOF)) == 'sign_in_failed'
    assert login_error_message(['sign_in_failed']) == LOGIN_ERRORS['sign_in_failed']


def test_local_expired_challenge_and_lockout_keep_fixed_feedback(local_client):
    data = {'username': 'alice.reviewer', 'password': 'wrong-synthetic-password',
            'login_challenge': 'expired', 'next': '/matters/new'}
    expired = local_client.post('/auth/local', data=data, follow_redirects=False)
    assert parse_qs(urlsplit(expired.headers['location']).query)['error'] == ['login_expired']
    assert LOGIN_ERRORS['login_expired'] in local_client.get(expired.headers['location']).text
    for _ in range(11):
        data['login_challenge'] = _challenge(local_client.get('/auth/login').text)
        denied = local_client.post('/auth/local', data=data, follow_redirects=False)
    assert parse_qs(urlsplit(denied.headers['location']).query)['error'] == ['local_throttled']
    page = local_client.get(denied.headers['location'])
    assert LOGIN_ERRORS['local_throttled'] in page.text
    assert local_client.cookies.get(LOGIN_CHALLENGE_COOKIE)
    assert local_client.cookies.get(SESSION_COOKIE) is None


def test_preview_expired_and_unknown_identity_keep_fixed_feedback(tmp_path):
    app = create_workbench_app(tmp_path / 'runtime', generator=UnavailableGenerator(),
                              auth_mode='preview', answer_workers=1)
    with TestClient(app) as client:
        for challenge, expected in [('expired', 'preview_expired'), (None, 'preview_identity')]:
            challenge = challenge or _challenge(client.get('/auth/login').text)
            response = client.post('/auth/login', data={'identity_subject': 'unknown-synthetic',
                'login_challenge': challenge}, follow_redirects=False)
            assert parse_qs(urlsplit(response.headers['location']).query)['error'] == [expected]
            assert LOGIN_ERRORS[expected] in client.get(response.headers['location']).text
            assert client.cookies.get(SESSION_COOKIE) is None


@pytest.mark.parametrize("provider_text", [SPOOF, SPOOF * 4, "", "x" * 10000 + SPOOF])
def test_oidc_provider_text_is_not_reflected_and_transaction_is_consumed(tmp_path, provider_text):
    settings = _settings()
    app = create_workbench_app(tmp_path / 'runtime', generator=UnavailableGenerator(), auth_mode='oidc',
                              secure_cookie=True, oidc_settings=settings,
                              oidc_client=FakeOidcProvider(settings), answer_workers=1)
    with TestClient(app, base_url=settings.external_origin) as client:
        state = _begin(client)
        response = client.get('/auth/oidc/callback', params={'state': state, 'error': provider_text}, follow_redirects=False)
        assert response.headers['location'] == '/auth/login?error=oidc_incomplete_login'
        assert client.cookies.get(OIDC_STATE_COOKIE) is None
        assert client.cookies.get(SESSION_COOKIE) is None
        assert LOGIN_ERRORS['oidc_incomplete_login'] in client.get(response.headers['location']).text
        assert 'synthetic-attacker' not in response.text + str(response.headers)
        replay = client.get('/auth/oidc/callback', params={'state': state, 'code': 'synthetic-code'}, follow_redirects=False)
        assert replay.headers['location'] == '/auth/login?error=oidc_expired'

        async def unknown_failure(*args, **kwargs):
            raise OidcAuthenticationError(SPOOF)
        app.state.identity.begin_oidc_login = unknown_failure
        denied = client.get('/auth/oidc/start', follow_redirects=False)
        assert 'error=sign_in_failed' in denied.headers['location']
        assert 'synthetic-attacker' not in str(denied.headers) + client.get(denied.headers['location']).text


def test_kerberos_ignores_caller_message_and_retains_fixed_denial(tmp_path):
    app = create_workbench_app(tmp_path / 'runtime', generator=UnavailableGenerator(), auth_mode='kerberos',
                              secure_cookie=True, kerberos_settings=kerberos_settings(), answer_workers=1)
    with TestClient(app, base_url='https://case.example.test') as client:
        page = client.get('/auth/login', params={'error': SPOOF * 4})
        assert page.status_code == 401
        assert LOGIN_ERRORS['windows_unverified'] in page.text
        assert 'synthetic-attacker' not in page.text
        assert client.cookies.get(SESSION_COOKIE) is None


def test_authentication_exception_vocabulary_is_complete():
    root = Path(__file__).resolve().parents[1] / 'src/case_intelligence'
    for filename in ('identity.py', 'identity_oidc_login.py', 'case_oidc.py'):
        tree = ast.parse((root / filename).read_text())
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id in {'LocalAuthenticationError', 'KerberosAuthenticationError', 'OidcAuthenticationError'}
                    and node.args):
                continue
            value = node.args[0]
            if isinstance(value, ast.Constant):
                message = value.value
            elif isinstance(value, ast.JoinedStr):
                assert all(isinstance(part, ast.Constant) or
                           isinstance(part, ast.FormattedValue) and isinstance(part.value, ast.Name)
                           and part.value.id == 'PRODUCT_NAME' for part in value.values)
                message = ''.join(part.value if isinstance(part, ast.Constant) else PRODUCT_NAME for part in value.values)
            else:
                pytest.fail(f'{filename}:{node.lineno}: use a fixed authentication message')
            assert message in LOGIN_ERRORS.values(), (filename, node.lineno)


@pytest.mark.parametrize('code,message', LOGIN_ERRORS.items())
def test_server_exception_redirect_uses_code_and_retains_wording(tmp_path, code, message):
    settings = _settings()
    app = create_workbench_app(tmp_path / 'runtime', generator=UnavailableGenerator(), auth_mode='oidc',
                              secure_cookie=True, oidc_settings=settings,
                              oidc_client=FakeOidcProvider(settings), answer_workers=1)
    async def fixed_failure(*args, **kwargs):
        raise OidcAuthenticationError(message)
    app.state.identity.begin_oidc_login = fixed_failure
    with TestClient(app, base_url=settings.external_origin) as client:
        response = client.get('/auth/oidc/start', follow_redirects=False)
        assert parse_qs(urlsplit(response.headers['location']).query)['error'] == [code]
        assert message not in response.headers['location']
        assert message in client.get(response.headers['location']).text
        assert client.cookies.get(SESSION_COOKIE) is None
