"""Synthetic live identity changes at lifecycle service and purge boundaries."""
from datetime import datetime, timedelta, timezone
import hashlib
import sqlite3
import uuid

import pytest

from case_intelligence.case_lifecycle_authority import (
    install_current_session_authority, require_current_session_authority)
from case_intelligence.identity import IdentityService, KerberosSettings, LocalAccountSettings, OidcSettings
from case_intelligence.local_accounts import LocalAccountRepository
from case_intelligence.workspace_store import WorkspaceProblem, WorkspaceStore


PASSWORD = 'synthetic-authority-password'
OPERATOR = 'synthetic-operator'
MODE = 'delete_without_export'


@pytest.fixture
def repository(tmp_path):
    now = [datetime(2026, 1, 1, tzinfo=timezone.utc)]
    store = WorkspaceStore(tmp_path / 'synthetic.sqlite', clock=lambda: now[0])
    owner = store.upsert_principal('test', 'owner', 'Synthetic Owner', 'owner')
    member = store.upsert_principal('test', 'member', 'Synthetic Member', 'member')
    matter = store.create_matter('Synthetic authority matter', '', owner.principal_id)
    store.add_member(matter.matter_id, member.principal_id, owner.principal_id)
    yield store, matter, owner.principal_id, member.principal_id, now
    store.close()


def session(store, actor, *, administrator=False, method='test'):
    expires = store._timestamp(store.current_time() + timedelta(days=30))
    return store.create_session(actor, hashlib.sha256(uuid.uuid4().bytes).hexdigest(),
        method, expires, expires, frozenset({'administrator'}) if administrator else frozenset())


def prepare_operation(store, matter, owner, owner_session, now, operation):
    repo = store.case_lifecycle
    case = None
    if operation not in {'check_in', 'begin_exit'}:
        case = repo.begin_exit(matter.slug, owner, session_id=owner_session.session_id,
            mode='export_exit' if operation == 'register_export' else MODE)
    nonce = None
    if operation in {'issue_confirmation', 'claim_purge'}:
        now[0] += timedelta(days=7)
    if operation == 'claim_purge':
        nonce = repo.issue_confirmation(matter.slug, owner, owner_session.session_id, mode=MODE)

    def invoke(actor, session_id, administrator_override=False):
        kwargs = dict(session_id=session_id, administrator_override=administrator_override)
        if operation == 'check_in':
            return repo.check_in(matter.matter_id, actor, **kwargs)
        if operation == 'begin_exit':
            return repo.begin_exit(matter.slug, actor, mode=MODE, **kwargs)
        if operation == 'cancel_exit':
            return repo.cancel_exit(matter.slug, actor, case.exit_id, **kwargs)
        if operation == 'register_export':
            return repo.register_export(matter.matter_id, actor, case.exit_id,
                'full-export-' + 'a' * 32, 'b' * 64, 'c' * 64, **kwargs)
        if operation == 'issue_confirmation':
            return repo.issue_confirmation(matter.slug, actor, mode=MODE, **kwargs)
        return repo.claim_purge(matter.slug, actor, matter.display_name,
            token=nonce.token, mode=MODE, no_backup_acknowledged=True, source_count=0, **kwargs)
    return invoke


@pytest.mark.parametrize(('operation', 'invalid'), [
    (operation, invalid)
    for operation in ('check_in', 'begin_exit', 'cancel_exit', 'register_export', 'issue_confirmation', 'claim_purge')
    for invalid in ('missing', 'revoked', 'expired', 'wrong_actor', 'member_override')
    if (operation, invalid) != ('check_in', 'missing')
])
def test_mutators_reject_missing_or_stale_authority(repository, operation, invalid):
    store, matter, owner, member, now = repository
    owner_session = session(store, owner)
    invoke = prepare_operation(store, matter, owner, owner_session, now, operation)
    actor, session_id, override = owner, owner_session.session_id, False
    if invalid == 'missing':
        session_id = None
    elif invalid == 'revoked':
        store.revoke_session(session_id)
    elif invalid == 'expired':
        now[0] += timedelta(days=31)
    elif invalid == 'wrong_actor':
        session_id = session(store, member).session_id
    else:
        actor, session_id, override = member, session(store, member).session_id, True
    before = store.connection.total_changes
    with pytest.raises((KeyError, WorkspaceProblem)):
        invoke(actor, session_id, override)
    assert store.connection.total_changes == before
    assert not store.connection.in_transaction
    assert store.matter_lifecycle(matter.matter_id).state != 'purging'


def test_owner_check_in_compatibility_does_not_grant_sessionless_deletion(repository):
    store, matter, owner, member, _ = repository
    assert store.case_lifecycle.check_in(matter.matter_id, owner).mode == 'ongoing'
    with pytest.raises(KeyError):
        store.case_lifecycle.check_in(matter.matter_id, member)
    with pytest.raises(WorkspaceProblem):
        store.case_lifecycle.check_in(matter.matter_id, member, administrator_override=True)
    with pytest.raises(WorkspaceProblem):
        store.case_lifecycle.begin_exit(matter.slug, owner, mode=MODE)
    assert store.matter_lifecycle(matter.matter_id).state == 'active'


@pytest.mark.parametrize('administrator', [False, True])
def test_repository_only_persisted_test_sessions_can_complete_explicit_no_export_exit(repository, administrator):
    store, matter, owner, member, now = repository
    actor = member if administrator else owner
    acting_session = session(store, actor, administrator=administrator)
    kwargs = dict(session_id=acting_session.session_id, administrator_override=administrator)
    store.case_lifecycle.begin_exit(matter.slug, actor, mode=MODE, **kwargs)
    now[0] += timedelta(days=7)
    nonce = store.case_lifecycle.issue_confirmation(matter.slug, actor, mode=MODE, **kwargs)
    _, lifecycle = store.case_lifecycle.claim_purge(matter.slug, actor, matter.display_name,
        token=nonce.token, mode=MODE, no_backup_acknowledged=True, source_count=0, **kwargs)
    assert store.case_lifecycle.validate_purge(matter.matter_id, lifecycle.purge_id).mode == MODE


@pytest.mark.parametrize(('provider', 'method'), [
    ('local', 'local'), ('local', 'test'), ('oidc-synthetic', 'oidc'),
    ('oidc-synthetic', 'test'), ('kerberos-synthetic', 'spnego'), ('kerberos-synthetic', 'test')])
@pytest.mark.parametrize('administrator', [False, True])
def test_bare_repository_requires_active_provider_for_real_identities(repository, provider, method, administrator):
    store, _, _, _, _ = repository
    # Worker/service stores may receive a live principal-eligibility callback;
    # it does not supply session revision or administrator authority.
    store.principal_enabled = lambda _provider, _subject: True
    principal = store.upsert_principal(provider, 'synthetic-owner', 'Synthetic Owner', 'owner')
    matter = store.create_matter('Synthetic provider boundary', '', principal.principal_id)
    stored = session(store, principal.principal_id, administrator=administrator, method=method)
    assert not hasattr(store, '_case_session_authority')
    with pytest.raises(WorkspaceProblem, match='identity-provider authority is unavailable'):
        store.case_lifecycle.begin_exit(matter.slug, principal.principal_id, mode=MODE,
            session_id=stored.session_id, administrator_override=administrator)
    assert store.matter_lifecycle(matter.matter_id).state == 'active'


@pytest.mark.parametrize('invalid', ['revoked', 'expired', 'principal_disabled', 'owner_grant_removed', 'admin_demoted'])
def test_claim_does_not_authorize_execution_after_authority_is_lost(repository, invalid):
    store, matter, owner, member, now = repository
    administrator = invalid == 'admin_demoted'
    actor = member if administrator else owner
    acting_session = session(store, actor, administrator=administrator)
    kwargs = dict(session_id=acting_session.session_id, administrator_override=administrator)
    store.case_lifecycle.begin_exit(matter.slug, actor, mode=MODE, **kwargs)
    now[0] += timedelta(days=7)
    nonce = store.case_lifecycle.issue_confirmation(matter.slug, actor, mode=MODE, **kwargs)
    _, lifecycle = store.case_lifecycle.claim_purge(matter.slug, actor, matter.display_name,
        token=nonce.token, mode=MODE, no_backup_acknowledged=True, source_count=0, **kwargs)
    if invalid == 'revoked':
        store.revoke_session(acting_session.session_id)
    elif invalid == 'expired':
        now[0] += timedelta(days=31)
    else:
        with store._lock, store.connection:
            if invalid == 'principal_disabled':
                store.connection.execute('UPDATE workbench_principal SET active=0 WHERE principal_id=?', (actor,))
            elif invalid == 'owner_grant_removed':
                store.connection.execute("UPDATE workbench_matter_membership SET state='revoked' WHERE matter_id=? AND principal_id=?", (matter.matter_id, actor))
            else:
                store.connection.execute("UPDATE workbench_session SET application_roles='[]' WHERE session_id=?", (acting_session.session_id,))
    for operation in (
        lambda: store.case_lifecycle.validate_purge(matter.matter_id, lifecycle.purge_id),
        lambda: store.complete_matter_purge(matter.matter_id, lifecycle.purge_id),
    ):
        with pytest.raises((KeyError, WorkspaceProblem)):
            operation()
        assert store.matter_lifecycle(matter.matter_id).state == 'purging'
        assert store.connection.execute('SELECT COUNT(*) FROM workbench_conversation WHERE matter_id=?',
                                        (matter.matter_id,)).fetchone()[0] == 1
        assert store._case_cleanup_matter is None


@pytest.fixture
def local_identity(tmp_path):
    accounts = LocalAccountRepository(tmp_path / 'accounts/accounts.json')
    accounts.initialize('synthetic.admin', 'Synthetic Administrator', PASSWORD, actor=OPERATOR)
    accounts.create('backup.admin', 'Synthetic Backup Administrator', PASSWORD, administrator=True, actor=OPERATOR)
    accounts.create('synthetic.owner', 'Synthetic Owner', PASSWORD, actor=OPERATOR)
    runtime = tmp_path / 'runtime'
    runtime.mkdir()
    now = [datetime(2026, 1, 1, tzinfo=timezone.utc)]
    store = WorkspaceStore(runtime / 'synthetic.sqlite', clock=lambda: now[0])
    identity = IdentityService(store, runtime, auth_mode='local', secure_cookie=True,
        local_settings=LocalAccountSettings(accounts.path), clock=lambda: now[0])
    install_current_session_authority(identity)
    yield identity, accounts, now
    store.close()


@pytest.mark.parametrize('change', ['demote', 'disable', 'unavailable'])
def test_local_provider_rechecks_live_account_even_when_session_still_has_admin_role(local_identity, change, monkeypatch):
    identity, accounts, _ = local_identity
    context, _ = identity.login_local('synthetic.admin', PASSWORD)
    require_current_session_authority(identity.store, context.principal_id, context.session.session_id, True)
    if change == 'demote':
        accounts.set_administrator('synthetic.admin', False, actor=OPERATOR)
    elif change == 'disable':
        accounts.set_enabled('synthetic.admin', False, actor=OPERATOR)
    else:
        def unavailable(_):
            raise OSError('synthetic unavailable account source')
        monkeypatch.setattr(identity.local_settings, 'account', unavailable)
    before = identity.store.connection.total_changes
    with pytest.raises(WorkspaceProblem):
        require_current_session_authority(identity.store, context.principal_id, context.session.session_id, True)
    assert identity.store.connection.total_changes == before
    assert identity.store.session_by_id(context.session.session_id).revoked_at is None


def test_local_admin_demotion_after_claim_blocks_database_cleanup(local_identity):
    identity, accounts, now = local_identity
    owner, _ = identity.login_local('synthetic.owner', PASSWORD)
    admin, _ = identity.login_local('synthetic.admin', PASSWORD)
    store = identity.store
    matter = store.create_matter('Synthetic administrator exit', '', owner.principal_id)
    store.case_lifecycle.begin_exit(matter.slug, admin.principal_id, mode=MODE,
        session_id=admin.session.session_id, administrator_override=True)
    now[0] += timedelta(days=7)
    admin, _ = identity.login_local('synthetic.admin', PASSWORD)
    kwargs = dict(session_id=admin.session.session_id, administrator_override=True)
    nonce = store.case_lifecycle.issue_confirmation(matter.slug, admin.principal_id, mode=MODE, **kwargs)
    _, lifecycle = store.case_lifecycle.claim_purge(matter.slug, admin.principal_id, matter.display_name,
        token=nonce.token, mode=MODE, no_backup_acknowledged=True, source_count=0, **kwargs)
    accounts.set_administrator('synthetic.admin', False, actor=OPERATOR)
    with pytest.raises(WorkspaceProblem, match='administrator authority'):
        store.complete_matter_purge(matter.matter_id, lifecycle.purge_id)
    assert store.matter_lifecycle(matter.matter_id).state == 'purging'


def change_local_revision(accounts, change):
    if change == 'password':
        accounts.change_password('synthetic.admin', 'synthetic-replacement-password', actor=OPERATOR)
        return 'synthetic-replacement-password'
    if change == 'disable-enable':
        accounts.set_enabled('synthetic.admin', False, actor=OPERATOR)
        accounts.set_enabled('synthetic.admin', True, actor=OPERATOR)
    else:
        accounts.set_administrator('synthetic.admin', False, actor=OPERATOR)
        accounts.set_administrator('synthetic.admin', True, actor=OPERATOR)
    return PASSWORD


@pytest.mark.parametrize('change', ['password', 'disable-enable', 'demote-promote'])
@pytest.mark.parametrize('administrator', [False, True])
def test_local_revision_changes_never_revive_direct_service_authority(local_identity, change, administrator):
    identity, accounts, _ = local_identity
    context, token = identity.login_local('synthetic.admin', PASSWORD)
    store = identity.store
    matter = store.create_matter('Synthetic local revision matter', '', context.principal_id)
    new_password = change_local_revision(accounts, change)
    # No intervening resolution can persist revocation and mask this boundary.
    assert store.session_by_id(context.session.session_id).revoked_at is None
    assert accounts.read()['synthetic.admin'].enabled
    assert 'administrator' in accounts.read()['synthetic.admin'].roles
    assert identity.resolve(token, read_only=True) is None
    with pytest.raises(WorkspaceProblem, match='Sign in again'):
        store.case_lifecycle.begin_exit(matter.slug, context.principal_id, mode=MODE,
            session_id=context.session.session_id, administrator_override=administrator)
    assert store.matter_lifecycle(matter.matter_id).state == 'active'
    fresh, _ = identity.login_local('synthetic.admin', new_password)
    store.case_lifecycle.begin_exit(matter.slug, fresh.principal_id, mode=MODE,
        session_id=fresh.session.session_id, administrator_override=administrator)
    assert store.matter_lifecycle(matter.matter_id).state == 'closing'


@pytest.mark.parametrize('change', ['password', 'disable-enable', 'demote-promote'])
def test_local_revision_change_after_claim_blocks_direct_cleanup(local_identity, change):
    identity, accounts, now = local_identity
    context, _ = identity.login_local('synthetic.admin', PASSWORD)
    store = identity.store
    matter = store.create_matter('Synthetic local revision purge', '', context.principal_id)
    store.case_lifecycle.begin_exit(matter.slug, context.principal_id, mode=MODE,
        session_id=context.session.session_id)
    now[0] += timedelta(days=7)
    context, _ = identity.login_local('synthetic.admin', PASSWORD)
    nonce = store.case_lifecycle.issue_confirmation(matter.slug, context.principal_id,
        context.session.session_id, mode=MODE)
    _, lifecycle = store.case_lifecycle.claim_purge(matter.slug, context.principal_id,
        matter.display_name, token=nonce.token, session_id=context.session.session_id,
        mode=MODE, no_backup_acknowledged=True, source_count=0)
    change_local_revision(accounts, change)
    assert store.session_by_id(context.session.session_id).revoked_at is None
    with pytest.raises(WorkspaceProblem, match='Sign in again'):
        store.complete_matter_purge(matter.matter_id, lifecycle.purge_id)
    assert store.matter_lifecycle(matter.matter_id).state == 'purging'
    assert store._case_cleanup_matter is None


def test_legacy_local_session_requires_fresh_login_for_lifecycle(local_identity):
    identity, _, _ = local_identity
    context, token = identity.login_local('synthetic.admin', PASSWORD)
    store = identity.store
    with store._lock, store.connection:
        store.connection.execute('DELETE FROM workbench_case_session_binding WHERE session_id=?',
                                 (context.session.session_id,))
    assert identity.resolve(token, read_only=True) is not None
    with pytest.raises(WorkspaceProblem, match='Sign in again'):
        require_current_session_authority(store, context.principal_id, context.session.session_id, True)
    fresh, _ = identity.login_local('synthetic.admin', PASSWORD)
    require_current_session_authority(store, fresh.principal_id, fresh.session.session_id, True)


def test_local_session_and_binding_are_atomic_and_do_not_store_cookie_binding(local_identity):
    identity, accounts, _ = local_identity
    store = identity.store
    context, token = identity.login_local('synthetic.admin', PASSWORD)
    row = store.connection.execute('SELECT binding_digest FROM workbench_case_session_binding WHERE session_id=?',
                                    (context.session.session_id,)).fetchone()
    binding = identity._local_session_binding(accounts.read()['synthetic.admin'])
    assert row['binding_digest'] == hashlib.sha256(binding.encode('ascii')).hexdigest()
    assert row['binding_digest'] != binding and row['binding_digest'] not in token
    before = store.connection.execute('SELECT COUNT(*) FROM workbench_session').fetchone()[0]
    with store._lock, store.connection:
        store.connection.execute('CREATE TEMP TRIGGER synthetic_binding_failure '
            'BEFORE INSERT ON workbench_case_session_binding BEGIN '
            "SELECT RAISE(ABORT, 'synthetic binding failure'); END")
    with pytest.raises(sqlite3.IntegrityError, match='synthetic binding failure'):
        identity.login_local('synthetic.admin', PASSWORD)
    assert store.connection.execute('SELECT COUNT(*) FROM workbench_session').fetchone()[0] == before
    assert not store.connection.in_transaction


def test_local_binding_survives_independent_identity_reopen(local_identity):
    identity, accounts, now = local_identity
    context, _ = identity.login_local('synthetic.admin', PASSWORD)
    runtime = identity.runtime_dir
    second_store = WorkspaceStore(runtime / 'synthetic.sqlite', clock=lambda: now[0])
    second = IdentityService(second_store, runtime, auth_mode='local', secure_cookie=True,
        local_settings=LocalAccountSettings(accounts.path), clock=lambda: now[0])
    install_current_session_authority(second)
    try:
        require_current_session_authority(second_store, context.principal_id, context.session.session_id, True)
        change_local_revision(accounts, 'disable-enable')
        with pytest.raises(WorkspaceProblem, match='Sign in again'):
            require_current_session_authority(second_store, context.principal_id, context.session.session_id, True)
    finally:
        second_store.close()


@pytest.mark.parametrize('method', ['test', 'preview', 'oidc', 'spnego'])
def test_local_provider_rejects_other_authentication_methods(local_identity, method):
    identity, _, _ = local_identity
    owner, _ = identity.login_local('synthetic.owner', PASSWORD)
    wrong = session(identity.store, owner.principal_id, method=method)
    with pytest.raises(WorkspaceProblem, match='current identity provider'):
        require_current_session_authority(identity.store, owner.principal_id, wrong.session_id, False)


@pytest.mark.parametrize('change', ['demote', 'remove_admission', 'resolver_failure'])
def test_kerberos_rechecks_current_groups_and_admission(tmp_path, change):
    runtime = tmp_path / 'runtime'
    runtime.mkdir()
    store = WorkspaceStore(runtime / 'synthetic.sqlite')
    groups = {'synthetic-reviewers', 'synthetic-administrators'}
    def resolve_groups(_):
        if groups is None:
            raise OSError('synthetic directory unavailable')
        return groups
    settings = KerberosSettings(realm='EXAMPLE.TEST', proxy_secret='synthetic-proxy-secret-' + 'a' * 32,
        allowed_groups=frozenset({'synthetic-reviewers'}),
        administrator_groups=frozenset({'synthetic-administrators'}))
    identity = IdentityService(store, runtime, auth_mode='kerberos', secure_cookie=True,
        kerberos_settings=settings, kerberos_group_resolver=resolve_groups,
        kerberos_profile_resolver=lambda subject: ('Synthetic Administrator', subject))
    install_current_session_authority(identity)
    try:
        context, _ = identity.login_kerberos('synthetic.admin@EXAMPLE.TEST', settings.proxy_secret)
        require_current_session_authority(store, context.principal_id, context.session.session_id, True)
        groups = {'synthetic-reviewers'} if change == 'demote' else (set() if change == 'remove_admission' else None)
        with pytest.raises(WorkspaceProblem):
            require_current_session_authority(store, context.principal_id, context.session.session_id, True)
        if change == 'demote':
            require_current_session_authority(store, context.principal_id, context.session.session_id, False)
        else:
            with pytest.raises(WorkspaceProblem):
                require_current_session_authority(store, context.principal_id, context.session.session_id, False)
    finally:
        store.close()


def test_preview_synthetic_admin_is_explicitly_scoped_to_preview_provider(tmp_path):
    runtime = tmp_path / 'runtime'
    runtime.mkdir()
    store = WorkspaceStore(runtime / 'synthetic.sqlite')
    identity = IdentityService(store, runtime, auth_mode='preview')
    install_current_session_authority(identity)
    try:
        principal = store.get_principal('development-alex-rivera')
        context, _ = identity._issue_session(principal, 'preview', frozenset({'administrator'}))
        require_current_session_authority(store, principal.principal_id, context.session.session_id, True)
        wrong = session(store, principal.principal_id, administrator=True)
        with pytest.raises(WorkspaceProblem, match='current identity provider'):
            require_current_session_authority(store, principal.principal_id, wrong.session_id, True)
    finally:
        store.close()


def test_oidc_owner_is_allowed_but_admin_needs_current_provider_authority(tmp_path):
    runtime = tmp_path / 'runtime'
    runtime.mkdir()
    store = WorkspaceStore(runtime / 'synthetic.sqlite')
    settings = OidcSettings(issuer='https://identity.example.test', external_origin='https://workbench.example.test',
        client_id='synthetic-client', client_secret='synthetic-secret',
        administrator_groups=frozenset({'synthetic-administrators'}))
    identity = IdentityService(store, runtime, auth_mode='oidc', secure_cookie=True, oidc_settings=settings)
    install_current_session_authority(identity)
    try:
        principal = store.upsert_principal(settings.provider_key, 'synthetic-owner', 'Synthetic Owner', 'owner')
        context, _ = identity._issue_session(principal, 'oidc', frozenset({'administrator'}))
        require_current_session_authority(store, principal.principal_id, context.session.session_id, False)
        with pytest.raises(WorkspaceProblem, match='cannot be verified'):
            require_current_session_authority(store, principal.principal_id, context.session.session_id, True)
    finally:
        store.close()
