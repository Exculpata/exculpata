"""Synthetic provider callbacks and central operation authority; no live IdP."""

from tests.fixture_write import fixture_write
import io
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit

from contextlib import ExitStack

import pytest
from fastapi.testclient import TestClient

from case_intelligence.generation import UnavailableGenerator
from case_intelligence.identity import OIDC_STATE_COOKIE, SESSION_COOKIE
from case_intelligence.managed_storage import StoragePolicy
from case_intelligence.store_records import WorkspaceProblem
from case_intelligence.workbench import create_workbench_app
from tests.test_oidc_authentication import FakeOidcProvider, _settings, _begin

MODE = 'delete_without_export'


class FreshProvider(FakeOidcProvider):
    def __init__(self, settings, clock):
        super().__init__(settings)
        self.clock = clock
        self.groups = ['synthetic-administrators']
        self.overrides = {}
        self.force_supported = True

    async def authorization_url(self, *, state, nonce, code_verifier, max_age=None):
        url = await super().authorization_url(state=state, nonce=nonce, code_verifier=code_verifier)
        self.authorization_calls[-1]['max_age'] = max_age
        return url + ('&max_age=0' if max_age == 0 and self.force_supported else '')

    async def authenticate(self, **kwargs):
        claims = await super().authenticate(**kwargs)
        now = int(self.clock[0].timestamp())
        claims.update(auth_time=now, iat=now, exp=now+3600)
        claims.update(self.overrides)
        return claims


def callback(client):
    state = client.cookies.get(OIDC_STATE_COOKIE)
    return client.get('/auth/oidc/callback', params={'code':'synthetic-stepup-code', 'state':state},
                      follow_redirects=False)


def signin(client):
    client.cookies.clear()
    _begin(client)
    response = callback(client)
    assert response.status_code == 303 and response.headers['location'] == '/matters/new'
    return context(client)


def context(client):
    return client.app.state.identity.resolve(client.cookies.get(SESSION_COOKIE), read_only=True)


def post(client, matter, action, **values):
    return client.post(f'/matters/{matter.slug}/{action}',
        data={'csrf_token':context(client).csrf_token, **values}, follow_redirects=False)


@pytest.fixture
def oidc_case(tmp_path, request):
    clock = [datetime(2026, 10, 13, 12, tzinfo=timezone.utc)]
    settings = _settings(administrator_groups=frozenset({'synthetic-administrators'}))
    provider = FreshProvider(settings, clock)
    app = create_workbench_app(tmp_path/'runtime', generator=UnavailableGenerator(), auth_mode='oidc',
        secure_cookie=True, oidc_settings=settings, oidc_client=provider, answer_workers=1,
        storage_policy=StoragePolicy(reserve_bytes=0))
    bench = app.state.workbench
    app.state.identity._clock = lambda: clock[0]
    bench.workspace._clock = lambda: clock[0]
    stack = ExitStack()
    request.addfinalizer(stack.close)
    client = stack.enter_context(TestClient(app, base_url=settings.external_origin))
    signin(client)
    owner = bench.workspace.upsert_principal(settings.provider_key, 'synthetic-other-owner', 'Synthetic Owner', 'owner')
    matter = bench.create_matter('Synthetic SSO closure', '', owner.principal_id)
    source = bench.source_store(matter)
    document, _ = source.store_stream('synthetic.txt', 'text/plain', io.BytesIO(b'Synthetic original.'))
    return client, bench, matter, provider, clock, source, document


def begin(client, bench, matter, mode=MODE):
    before = context(client)
    response = post(client, matter, 'close/begin', mode=mode)
    assert response.status_code == 302
    assert parse_qs(urlsplit(response.headers['location']).query)['max_age'] == ['0']
    assert bench.workspace.matter_lifecycle(matter.matter_id).state == 'active'
    assert callback(client).headers['location'] == f'/matters/{matter.slug}/close'
    assert context(client).session.session_id != before.session.session_id
    assert bench.workspace.session_by_id(before.session.session_id).revoked_at is not None
    response = post(client, matter, 'close/begin', mode=mode)
    assert response.status_code == 303 and 'error=' not in response.headers['location']
    assert bench.workspace.case_lifecycle.current_exit(matter.matter_id).mode == mode


def confirmation(client, bench, matter, clock, *, mode=MODE, advance=True):
    if advance:
        clock[0] += timedelta(days=7, seconds=1)
        signin(client)
    current = bench.workspace.case_lifecycle.current_exit(matter.matter_id)
    values = {'mode':mode, 'export_id':current.export_id or ''}
    response = post(client, matter, 'close/confirm', **values)
    assert response.status_code == 302, response.headers.get('location')
    assert callback(client).headers['location'] == f'/matters/{matter.slug}/close'
    response = post(client, matter, 'close/confirm', **values)
    assert response.status_code == 200, response.headers.get('location')
    token = re.search(r'name="token" value="([^"]+)"', response.text).group(1)
    return {**values, 'token':token, 'confirmed_name':matter.display_name,
            'acknowledge':'yes', 'no_backup_acknowledged':'yes', 'export_acknowledged':'yes'}


@pytest.mark.parametrize('mode', ['export_exit', MODE])
def test_oidc_nonowner_admin_completes_both_exit_paths_with_distinct_fresh_actions(oidc_case, mode):
    client, bench, matter, provider, clock, source, document = oidc_case
    begin(client, bench, matter, mode)
    if mode == 'export_exit':
        response = post(client, matter, 'close/export')
        assert 'error=' not in response.headers['location']
        assert client.get(f'/matters/{matter.slug}/close/export').status_code == 200
    values = confirmation(client, bench, matter, clock, mode=mode)
    assert [call['max_age'] for call in provider.authorization_calls].count(0) == 2
    response = post(client, matter, 'close', **values)
    assert response.headers['location'].startswith('/matters/new?'), response.headers['location']
    assert bench.workspace.matter_lifecycle(matter.matter_id).state == 'deleted'
    events = [event for event in bench.workspace.audit_events(matter.matter_id)
              if event.action == 'matter.exit_reauthenticate']
    assert len(events) == 2 and all(event.details == {'role':'administrator'} for event in events)


@pytest.mark.parametrize('failure', ['missing','old','future','boolean','string','nan','overflow','expired','expires_before_auth','issued_before','subject','groups','revoked','disabled','config','unsupported'])
def test_forced_reauth_rejects_unsupported_or_stale_claims_without_starting_closure(oidc_case, failure):
    client, bench, matter, provider, clock, _, _ = oidc_case
    if failure == 'unsupported':
        provider.force_supported = False
    response = post(client, matter, 'close/begin', mode=MODE)
    if failure == 'unsupported':
        assert response.status_code == 303 and 'error=' in response.headers['location']
        return
    assert response.status_code == 302
    now = int(clock[0].timestamp())
    if failure in {'missing','old','future','boolean','string','nan','overflow'}:
        provider.overrides['auth_time'] = {'missing':None,'old':now-1,'future':now+61,
                                          'boolean':True,'string':str(now),'nan':float('nan'),
                                          'overflow':10**1000}[failure]
    elif failure == 'expired':
        provider.overrides['exp'] = now
    elif failure == 'expires_before_auth':
        provider.overrides.update(auth_time=now+1, iat=now+1, exp=now+0.5)
    elif failure == 'issued_before':
        provider.overrides['iat'] = now-1
    elif failure == 'subject':
        provider.subject = 'synthetic-wrong-person'
    elif failure == 'groups':
        provider.groups = []
    elif failure == 'revoked':
        bench.workspace.revoke_session(context(client).session.session_id)
    elif failure == 'disabled':
        with fixture_write(bench.workspace):
            bench.workspace.connection.execute('UPDATE workbench_principal SET active=0 WHERE principal_id=?',
                (context(client).principal_id,))
    elif failure == 'config':
        object.__setattr__(provider.settings, 'administrator_groups', frozenset({'different-administrators'}))
    response = callback(client)
    assert response.headers['location'].startswith('/auth/login?error=')
    assert bench.workspace.matter_lifecycle(matter.matter_id).state == 'active'
    assert bench.workspace.connection.execute('SELECT count(*) FROM workbench_case_oidc_authorization').fetchone()[0] == 0
    assert callback(client).headers['location'].startswith('/auth/login?error=')


def test_begin_grant_is_bound_to_subject_session_matter_action_mode_and_prospective_exit(oidc_case):
    client, bench, matter, _, _, _, _ = oidc_case
    old = context(client)
    assert post(client, matter, 'close/begin', mode=MODE).status_code == 302
    pending = dict(bench.workspace.connection.execute('SELECT * FROM workbench_case_oidc_request').fetchone())
    assert callback(client).status_code == 303
    fresh = context(client)
    neighbor = bench.create_matter('Synthetic other target', '', matter.owner_id)
    repo = bench.workspace.case_lifecycle
    for target, session, mode in [(neighbor, fresh.session.session_id, MODE),
                                 (matter, old.session.session_id, MODE),
                                 (matter, fresh.session.session_id, 'export_exit')]:
        with pytest.raises(WorkspaceProblem):
            repo.begin_exit(target.slug, fresh.principal_id, session_id=session, mode=mode, administrator_override=True)
    assert post(client, matter, 'close/begin', mode=MODE).status_code == 303
    assert repo.current_exit(matter.matter_id).exit_id == pending['exit_id']
    assert repo.current_exit(neighbor.matter_id) is None


@pytest.mark.parametrize('boundary', ['before_claim','after_claim','retry'])
def test_ten_minute_deadline_and_retry_require_fresh_authorization(oidc_case, boundary):
    client, bench, matter, _, clock, source, document = oidc_case
    begin(client, bench, matter)
    values = confirmation(client, bench, matter, clock)
    ctx = context(client)
    if boundary == 'before_claim':
        clock[0] += timedelta(seconds=600)
        response = post(client, matter, 'close', **values)
        assert 'error=' in response.headers['location']
        assert bench.workspace.matter_lifecycle(matter.matter_id).state == 'closing'
        assert (source.files/document.stored_name).read_bytes() == b'Synthetic original.'
        return
    _, lifecycle = bench.begin_matter_purge(matter.slug, ctx.principal_id, matter.display_name,
        administrator_override=True, session_id=ctx.session.session_id, token=values['token'],
        mode=MODE, no_backup_acknowledged=True)
    if boundary == 'after_claim':
        clock[0] += timedelta(seconds=600)
        with pytest.raises(WorkspaceProblem):
            bench.execute_matter_purge(matter, lifecycle)
        assert (source.files/document.stored_name).read_bytes() == b'Synthetic original.'
        assert bench.workspace.matter_lifecycle(matter.matter_id).error_code == 'authority'
    else:
        bench.workspace.fail_matter_purge(matter.matter_id, lifecycle.purge_id, 'storage')
    with pytest.raises(WorkspaceProblem, match='specific closure action'):
        bench.workspace.case_lifecycle.issue_confirmation(matter.slug, ctx.principal_id,
            ctx.session.session_id, mode=MODE, administrator_override=True)
    values = confirmation(client, bench, matter, clock, advance=False)
    response = post(client, matter, 'close', **values)
    assert response.headers['location'].startswith('/matters/new?')
    assert bench.workspace.matter_lifecycle(matter.matter_id).purge_id == lifecycle.purge_id


def test_new_verified_login_revocation_restart_and_cancel_invalidate_operation_grants(oidc_case):
    client, bench, matter, provider, _, _, _ = oidc_case
    assert post(client, matter, 'close/begin', mode=MODE).status_code == 302
    assert callback(client).status_code == 303
    granted = context(client)
    provider.groups = []
    signin(client)
    with pytest.raises(WorkspaceProblem):
        bench.workspace.case_lifecycle.begin_exit(matter.slug, granted.principal_id,
            session_id=granted.session.session_id, mode=MODE, administrator_override=True)
    provider.groups = ['synthetic-administrators']
    signin(client)
    assert post(client, matter, 'close/begin', mode=MODE).status_code == 302
    assert callback(client).status_code == 303
    bench.workspace.case_lifecycle.discard_confirmations_on_startup()
    assert post(client, matter, 'close/begin', mode=MODE).status_code == 302
    assert callback(client).status_code == 303
    assert post(client, matter, 'close/begin', mode=MODE).status_code == 303
    current = bench.workspace.case_lifecycle.current_exit(matter.matter_id)
    assert post(client, matter, 'close/cancel', exit_id=current.exit_id).status_code == 303
    assert post(client, matter, 'close/begin', mode=MODE).status_code == 302


def test_owner_uses_owner_grant_and_ordinary_oidc_member_cannot_start_reauthentication(oidc_case):
    client, bench, matter, provider, _, _, _ = oidc_case
    provider.groups = []
    member = signin(client)
    bench.workspace.add_member(matter.matter_id, member.principal_id, matter.owner_id)
    calls = len(provider.authorization_calls)
    assert post(client, matter, 'close/begin', mode=MODE).status_code == 404
    assert len(provider.authorization_calls) == calls
    provider.subject = 'synthetic-other-owner'
    signin(client)
    assert post(client, matter, 'close/begin', mode=MODE).status_code == 303
    assert provider.authorization_calls[-1]['max_age'] is None
    assert bench.workspace.matter_lifecycle(matter.matter_id).state == 'closing'


def test_expired_or_replaced_requests_are_bounded_and_ordinary_login_remains_usable(oidc_case):
    client, bench, matter, _, clock, _, _ = oidc_case
    assert post(client, matter, 'close/begin', mode=MODE).status_code == 302
    first = client.cookies.get(OIDC_STATE_COOKIE)
    assert post(client, matter, 'close/begin', mode=MODE).status_code == 302
    assert bench.workspace.connection.execute('SELECT count(*) FROM workbench_case_oidc_request').fetchone()[0] == 1
    denied = client.get('/auth/oidc/callback', params={'state':first, 'code':'synthetic-code'}, follow_redirects=False)
    assert denied.headers['location'].startswith('/auth/login?error=')
    clock[0] += timedelta(minutes=6)
    signin(client)
    assert bench.workspace.connection.execute('SELECT count(*) FROM workbench_case_oidc_request').fetchone()[0] == 0
    assert bench.workspace.matter_lifecycle(matter.matter_id).state == 'active'


def test_same_subject_new_login_without_admission_still_invalidates_old_grant(oidc_case):
    client, bench, matter, provider, _, _, _ = oidc_case
    object.__setattr__(provider.settings, 'allowed_groups', frozenset({'synthetic-administrators'}))
    assert post(client, matter, 'close/begin', mode=MODE).status_code == 302
    assert callback(client).status_code == 303
    assert bench.workspace.connection.execute('SELECT count(*) FROM workbench_case_oidc_authorization').fetchone()[0] == 1
    provider.groups = []
    _begin(client)
    assert callback(client).headers['location'].startswith('/auth/login?error=')
    assert bench.workspace.connection.execute('SELECT count(*) FROM workbench_case_oidc_authorization').fetchone()[0] == 0
