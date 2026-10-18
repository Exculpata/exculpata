"""Synthetic real-session visibility, authority and lifecycle time presentation."""

from tests.fixture_write import fixture_write
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from case_intelligence.generation import UnavailableGenerator
from case_intelligence.identity import SESSION_COOKIE
from case_intelligence.managed_storage import StoragePolicy
from case_intelligence.workbench import create_workbench_app
from tests.test_case_lifecycle_web import login, post


OWNER = 'development-taylor-morgan'
MEMBER = 'development-jordan-lee'
ADMIN = 'development-alex-rivera'


@pytest.fixture
def notice_case(tmp_path):
    app = create_workbench_app(tmp_path / 'runtime', generator=UnavailableGenerator(),
        auth_mode='preview', answer_workers=1, storage_policy=StoragePolicy(reserve_bytes=0))
    bench = app.state.workbench
    clock = [datetime(2026, 10, 14, 0, 30, tzinfo=timezone.utc)]
    bench.workspace._clock = lambda: clock[0]
    app.state.identity._clock = lambda: clock[0]
    with TestClient(app) as client:
        login(client)
        matter = bench.create_matter('Synthetic closure target', '', OWNER)
        neighbor = bench.create_matter('Synthetic unrelated matter', '', OWNER)
        yield client, bench, matter, neighbor, clock


def begin_as_administrator(client, matter):
    # Grant the synthetic preview identity administrator authority in a real
    # persisted session. All HTTP requests still pass normal session and CSRF checks.
    identity = client.app.state.identity
    principal = identity.store.get_principal(ADMIN)
    context, token = identity._issue_session(principal, 'preview', frozenset({'administrator'}))
    client.cookies.clear()
    client.cookies.set(SESSION_COOKIE, token)
    response = post(client, matter, 'close/begin', context.csrf_token)
    assert response.status_code == 303 and 'error=' not in response.headers['location']
    return identity.store.case_lifecycle.current_exit(matter.matter_id)


def notice_events(bench, matter):
    return [event for event in bench.workspace.audit_events(matter.matter_id)
            if event.action == 'matter.exit_notice']


@pytest.mark.parametrize('access', ['owner', 'direct', 'group'])
def test_admin_closure_notifies_every_authorized_owner_or_member_without_new_authority(notice_case, access):
    client, bench, matter, neighbor, _ = notice_case
    if access == 'direct':
        bench.workspace.add_member(matter.matter_id, MEMBER, OWNER)
    elif access == 'group':
        group = bench.workspace.create_team_group('Synthetic review team', ADMIN, administrator_override=True)
        bench.workspace.set_team_group_member(group, MEMBER, ADMIN, present=True, administrator_override=True)
        bench.workspace.set_matter_group_grant(matter.matter_id, group, OWNER, present=True)
    current = begin_as_administrator(client, matter)
    actor = OWNER if access == 'owner' else MEMBER
    csrf = login(client, 'taylor-morgan' if access == 'owner' else 'jordan-lee')
    for path in ('/matters/new', '/matters/manage'):
        response = client.get(path)
        assert response.status_code == 200
        assert 'data-closure-notices' in response.text
        assert matter.display_name in response.text
        assert 'An administrator requested closure' in response.text
        assert 'The seven-day cancellation window is open.' in response.text
        assert f'data-lifecycle-local-time="{current.eligible_after}"' in response.text
        if access == 'owner':
            assert f'href="/matters/{matter.slug}/close"' in response.text
        else:
            assert 'Contact the matter owner or an administrator to request cancellation.' in response.text
            assert f'href="/matters/{matter.slug}/close"' not in response.text
            assert neighbor.display_name not in response.text
            assert 'You own this matter' not in response.text
    if access == 'owner':
        assert 'data-closure-notices' in client.get(f'/matters/{neighbor.slug}/settings').text
    else:
        assert client.get(f'/matters/{matter.slug}/close').status_code == 404
        assert post(client, matter, 'close/cancel', csrf, exit_id=current.exit_id).status_code == 404
    events = [event for event in notice_events(bench, matter) if event.actor_principal_id == actor]
    assert len(events) == 1
    assert events[0].session_id is not None
    assert events[0].object_type == 'case_exit' and events[0].object_id == current.exit_id
    assert events[0].details == {'role': 'owner' if access == 'owner' else 'member'}
    assert bench.workspace.matter_lifecycle(matter.matter_id).state == 'closing'


def test_notice_uses_live_membership_and_does_not_disclose_foreign_closures(notice_case):
    client, bench, matter, _, _ = notice_case
    group = bench.workspace.create_team_group('Synthetic team', ADMIN, administrator_override=True)
    bench.workspace.set_team_group_member(group, MEMBER, ADMIN, present=True, administrator_override=True)
    bench.workspace.set_matter_group_grant(matter.matter_id, group, OWNER, present=True)
    begin_as_administrator(client, matter)
    login(client, 'jordan-lee')
    assert 'data-closure-notices' in client.get('/matters/new').text
    bench.workspace.set_team_group_member(group, MEMBER, ADMIN, present=False, administrator_override=True)
    response = client.get('/matters/new')
    assert 'data-closure-notices' not in response.text
    assert matter.display_name not in response.text and matter.slug not in response.text
    assert len([event for event in notice_events(bench, matter) if event.actor_principal_id == MEMBER]) == 1


def test_notice_survives_window_then_cancel_removes_it_and_reentry_audits_new_generation(notice_case):
    client, bench, matter, _, clock = notice_case
    current = begin_as_administrator(client, matter)
    login(client)
    clock[0] += timedelta(days=7, seconds=1)
    csrf = login(client)
    response = client.get('/matters/new')
    assert 'The seven-day cancellation window has elapsed.' in response.text
    assert 'can still cancel until final deletion is confirmed' in response.text
    assert bench.workspace.matter_lifecycle(matter.matter_id).state == 'closing'
    assert post(client, matter, 'close/cancel', csrf, exit_id=current.exit_id).status_code == 303
    assert 'data-closure-notices' not in client.get('/matters/new').text
    assert bench.workspace.matter_lifecycle(matter.matter_id).state == 'active'
    assert post(client, matter, 'close/begin', csrf).status_code == 303
    response = client.get('/matters/new')
    assert 'The matter owner requested closure.' in response.text
    assert 'The seven-day cancellation window is open.' in response.text
    events = [event for event in notice_events(bench, matter) if event.actor_principal_id == OWNER]
    assert len(events) == 2 and len({event.object_id for event in events}) == 2


def test_lifecycle_times_keep_utc_fallback_and_expose_full_instant_for_viewer_zone(notice_case):
    client, bench, matter, _, clock = notice_case
    policy = bench.workspace.case_lifecycle.policy(matter.matter_id)
    before = policy.next_check_in_at
    for path in ('/matters/manage', f'/matters/{matter.slug}/settings'):
        response = client.get(path)
        assert f'datetime="{before}"' in response.text
        assert f'data-lifecycle-local-time="{before}"' in response.text
        assert '2027-01-12 00:30:00 UTC' in response.text
        assert '/static/lifecycle-times.js' in response.text
    assert bench.workspace.case_lifecycle.policy(matter.matter_id).next_check_in_at == before
    clock[0] = datetime(2027, 1, 12, 0, 29, 59, tzinfo=timezone.utc)
    assert bench.retention_projection(matter)['status'] == 'check_in_next_week'
    clock[0] += timedelta(seconds=1)
    assert bench.retention_projection(matter)['status'] == 'check_in_due'
    login(client)
    response = client.get(f'/matters/{matter.slug}/reports')
    assert response.status_code == 200
    assert 'Matter retention: set a review end date' not in response.text


@pytest.mark.parametrize('actor', ['owner', 'administrator', 'member'])
def test_unscheduled_invitation_changes_nothing_until_owner_or_admin_opts_in(notice_case, actor):
    client, bench, matter, _, clock = notice_case
    workspace = bench.workspace
    with fixture_write(workspace):
        workspace.connection.execute("UPDATE workbench_case_policy SET mode='unscheduled',next_check_in_at=NULL WHERE matter_id=?", (matter.matter_id,))
    before = workspace.case_lifecycle.policy(matter.matter_id)
    if actor == 'administrator':
        identity = client.app.state.identity
        ctx, token = identity._issue_session(workspace.get_principal(ADMIN), 'preview', frozenset({'administrator'}))
        client.cookies.clear()
        client.cookies.set(SESSION_COOKIE, token)
        csrf = ctx.csrf_token
    elif actor == 'member':
        workspace.add_member(matter.matter_id, MEMBER, OWNER)
        csrf = login(client, 'jordan-lee')
    else:
        csrf = login(client)
    for path in ('/matters/manage', f'/matters/{matter.slug}/settings'):
        page = client.get(path)
        assert page.status_code == 200
        assert ('data-check-in-invitation' in page.text) == (actor != 'member')
        assert workspace.case_lifecycle.policy(matter.matter_id) == before
    assert workspace.due_matter_retentions() == ()
    denied = client.post(f'/matters/{matter.slug}/check-in', data={}, follow_redirects=False)
    assert denied.status_code == 403
    assert workspace.case_lifecycle.policy(matter.matter_id) == before
    response = post(client, matter, 'check-in', csrf)
    if actor == 'member':
        assert response.status_code == 404
        assert workspace.case_lifecycle.policy(matter.matter_id) == before
    else:
        assert response.status_code == 303
        after = workspace.case_lifecycle.policy(matter.matter_id)
        assert after.mode == 'ongoing' and after.next_check_in_at == workspace._timestamp(clock[0]+timedelta(days=90))
        assert 'data-check-in-invitation' not in client.get(f'/matters/{matter.slug}/settings').text
        assert workspace.matter_lifecycle(matter.matter_id).state == 'active'
