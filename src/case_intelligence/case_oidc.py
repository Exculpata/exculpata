"""Ten-minute OIDC grants for one administrator closure operation.

Only validated provider claims can create a grant. The grant is not a directory
lookup: it accepts the explicitly approved bounded group-revocation window.
No provider tokens, credentials, or raw group claims are persisted here.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import math
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit

from .store_records import WorkspaceProblem

AUTHORIZATION_SECONDS = 600


def _instant(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00'))


def _digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def configuration_digest(identity):
    settings = identity.oidc_settings
    return _digest(json.dumps([settings.issuer, settings.client_id, settings.groups_claim,
        sorted(settings.allowed_groups), sorted(settings.administrator_groups),
        sorted(settings.scopes)], separators=(',', ':')))


def required(identity, context, matter):
    return (identity.auth_mode == 'oidc' and context.is_administrator
            and matter.owner_id != context.principal_id)


def _error():
    from .identity import OidcAuthenticationError
    return OidcAuthenticationError('Fresh organization sign-in could not authorize this closure. Start again with the same administrator account.')


def _target(workspace, matter_id, exit_id, mode, action):
    if mode not in {'export_exit', 'delete_without_export'} or action not in {'begin', 'purge'}:
        raise _error()
    current = workspace.case_lifecycle.current_exit(matter_id)
    state = workspace.matter_lifecycle(matter_id).state
    if action == 'begin':
        if state != 'active' or current is not None:
            raise _error()
    elif (current is None or current.exit_id != exit_id or current.mode != mode
          or state not in {'closing', 'purge_failed', 'purging'}):
        raise _error()


async def start(identity, context, matter, *, action, mode):
    """Called by a CSRF-protected owner/admin action, never a GET callback."""
    from .identity import OidcLoginStart
    workspace = identity.store
    if not required(identity, context, matter) or context.session is None:
        raise _error()
    with workspace._lock:
        workspace.case_lifecycle._session(context.principal_id, context.session.session_id, True)
        current = workspace.case_lifecycle.current_exit(matter.matter_id)
        exit_id = 'case-exit-' + uuid.uuid4().hex if action == 'begin' else (current.exit_id if current else '')
        _target(workspace, matter.matter_id, exit_id, mode, action)
    state = secrets.token_urlsafe(32)
    state_digest = identity.token_digest(state)
    try:
        url = await identity.oidc_client.authorization_url(state=state,
            nonce=identity._derive_oidc_value(b'nonce:', state),
            code_verifier=identity._derive_oidc_value(b'pkce:', state), max_age=0)
        if parse_qs(urlsplit(url).query).get('max_age') != ['0']:
            raise _error()
    except (TypeError, ValueError):
        # Providers/adapters that do not support forced reauthentication cannot
        # silently degrade to an ordinary sign-in.
        raise _error() from None
    now = workspace._now()
    with workspace.case_lifecycle._transaction():
        workspace.case_lifecycle._session(context.principal_id, context.session.session_id, True)
        _target(workspace, matter.matter_id, exit_id, mode, action)
        workspace.connection.execute('DELETE FROM workbench_oidc_transaction WHERE expires_at<=? OR consumed_at IS NOT NULL', (now,))
        workspace.connection.execute('DELETE FROM workbench_oidc_transaction WHERE state_digest IN '
            '(SELECT state_digest FROM workbench_case_oidc_request WHERE actor_id=?)', (context.principal_id,))
        workspace.connection.execute('DELETE FROM workbench_case_oidc_authorization WHERE expires_at<=?', (now,))
        workspace.connection.execute('INSERT INTO workbench_oidc_transaction VALUES (?,?,?,?,NULL)',
            (state_digest, f'/matters/{matter.slug}/close', now,
             workspace._timestamp(workspace.current_time() + timedelta(minutes=5))))
        workspace.connection.execute('INSERT INTO workbench_case_oidc_request VALUES (?,?,?,?,?,?,?,?,?,?)',
            (state_digest, context.principal_id, context.session.session_id,
             _digest(context.principal.provider_subject), configuration_digest(identity),
             matter.matter_id, exit_id, mode, action, now))
    return OidcLoginStart(url, state)


def take_request(identity, state):
    with identity.store.case_lifecycle._transaction():
        digest = identity.token_digest(state)
        row = identity.store.connection.execute(
            'SELECT * FROM workbench_case_oidc_request WHERE state_digest=?', (digest,)).fetchone()
        identity.store.connection.execute('DELETE FROM workbench_case_oidc_request WHERE state_digest=?', (digest,))
        return dict(row) if row else None


def validate_claims(identity, request, claims):
    if request is None:
        return
    try:
        identity.store.case_lifecycle._session(request['actor_id'], request['session_id'], True)
    except (WorkspaceProblem, KeyError):
        raise _error() from None
    now = identity.store.current_time().timestamp()
    auth_time, issued, expiry = (claims.get(key) for key in ('auth_time', 'iat', 'exp'))
    try:
        numeric_dates = all(type(value) in (int, float) and math.isfinite(value)
                            for value in (auth_time, issued, expiry))
    except OverflowError:
        numeric_dates = False
    if (not numeric_dates
            or auth_time < int(_instant(request['created_at']).timestamp())
            or auth_time > now + 1 or issued < auth_time or issued > now + 60
            or expiry <= auth_time
            or now >= min(auth_time + AUTHORIZATION_SECONDS, expiry)
            or not hmac.compare_digest(request['subject_digest'], _digest(str(claims.get('sub', ''))))
            or request['configuration_digest'] != configuration_digest(identity)):
        raise _error()


def invalidate_verified_subject(identity, subject):
    # Called only after signature/issuer/subject validation, even if newer group
    # claims subsequently refuse admission. Invalidation survives that refusal.
    with identity.store.case_lifecycle._transaction():
        identity.store.connection.execute(
            'DELETE FROM workbench_case_oidc_authorization WHERE actor_id IN '
            '(SELECT principal_id FROM workbench_principal WHERE provider=? AND provider_subject=?)',
            (identity.oidc_settings.provider_key, subject))


def finish(identity, request, context, claims):
    """Complete only a consumed, validated login; bind its newly issued session."""
    workspace = identity.store
    with workspace.case_lifecycle._transaction():
        # A new sign-in supersedes this actor's older lifecycle group evidence,
        # including when the fresh provider claims remove administrator status.
        workspace.connection.execute('DELETE FROM workbench_case_oidc_authorization WHERE actor_id=?',
                                     (context.principal_id,))
        if request is None:
            return
        validate_claims(identity, request, claims)
        if context.principal_id != request['actor_id'] or not context.is_administrator:
            raise _error()
        workspace.case_lifecycle._session(request['actor_id'], request['session_id'], True)
        _target(workspace, request['matter_id'], request['exit_id'], request['mode'], request['action'])
        if request['action'] == 'purge' and workspace.matter_lifecycle(request['matter_id']).state == 'purging':
            raise _error()
        expires = min(float(claims['auth_time']) + AUTHORIZATION_SECONDS, float(claims['exp']),
                      workspace.current_time().timestamp() + AUTHORIZATION_SECONDS,
                      _instant(context.session.absolute_expires_at).timestamp())
        workspace.connection.execute('INSERT INTO workbench_case_oidc_authorization VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
            ('case-oidc-' + uuid.uuid4().hex, context.principal_id, context.session.session_id,
             request['subject_digest'], request['configuration_digest'], request['matter_id'],
             request['exit_id'], request['mode'], request['action'],
             workspace._timestamp(datetime.fromtimestamp(claims['auth_time'], timezone.utc)),
             workspace._timestamp(datetime.fromtimestamp(expires, timezone.utc)), 'ready', None))
        # This proof starts a new browser session; possession of the previous
        # cookie cannot use it, and it cannot continue a previously claimed purge.
        workspace.connection.execute('UPDATE workbench_session SET revoked_at=? WHERE session_id=?',
                                     (workspace._now(), request['session_id']))
        workspace.case_lifecycle._audit('matter.exit_reauthenticate', request['matter_id'],
            context.principal_id, request['exit_id'], context.session.session_id,
            administrator_override=True)


def authorize(workspace, actor_id, session_id, administrator, matter_id, *, action,
              mode, exit_id=None, purge_id=None):
    """Called under the lifecycle lock/transaction, including direct/background calls."""
    identity = getattr(workspace, '_case_identity_service', None)
    if not administrator or identity is None or identity.auth_mode != 'oidc':
        return None
    principal = workspace.get_principal(actor_id)
    row = workspace.connection.execute(
        'SELECT * FROM workbench_case_oidc_authorization WHERE actor_id=? AND session_id=? '
        'AND matter_id=? AND action=? AND mode=?',
        (actor_id, session_id, matter_id, action, mode)).fetchone()
    if (row is None or _instant(row['expires_at']) <= workspace.current_time()
            or row['subject_digest'] != _digest(principal.provider_subject)
            or row['configuration_digest'] != configuration_digest(identity)
            or (exit_id is not None and row['exit_id'] != exit_id)
            or row['state'] != ('claimed' if purge_id is not None else 'ready')
            or row['purge_id'] != purge_id):
        raise WorkspaceProblem('Sign in again to authorize this specific closure action for at most ten minutes.')
    return row


def claim(workspace, authorization, *, purge_id=None):
    if authorization is not None:
        workspace.connection.execute("UPDATE workbench_case_oidc_authorization SET state='claimed',purge_id=? "
            "WHERE authorization_id=? AND state='ready'", (purge_id, authorization['authorization_id']))


def available(identity, context, matter, *, action, mode):
    if not required(identity, context, matter):
        return True
    current = identity.store.case_lifecycle.current_exit(matter.matter_id)
    try:
        with identity.store._lock:
            identity.store.case_lifecycle._session(context.principal_id, context.session.session_id, True)
            authorize(identity.store, context.principal_id, context.session.session_id, True,
                matter.matter_id, action=action, mode=mode,
                exit_id=current.exit_id if action == 'purge' and current else None)
        return True
    except (WorkspaceProblem, AttributeError, KeyError):
        return False


def selected_mode(identity, context, matter):
    if required(identity, context, matter):
        for mode in ('export_exit', 'delete_without_export'):
            if available(identity, context, matter, action='begin', mode=mode):
                return mode
    return 'export_exit'


def purge_records(workspace, matter_id):
    """Caller owns the authorized final content-cleanup transaction."""
    for table in ('workbench_case_oidc_request', 'workbench_case_oidc_authorization'):
        workspace.connection.execute(f'DELETE FROM {table} WHERE matter_id=?', (matter_id,))
