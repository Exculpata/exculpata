"""Read-only, provider-aware authority checks for explicit matter exits.

The repository validates its persisted session on every operation. This hook
also rechecks authority available from the application's configured provider.
HTTP callers must still resolve their raw cookie before each destructive phase:
the service-level local revision binding supplements the ordinary browser
session and request-authority checks; it never replaces possession of a cookie.
"""
from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime

from .store_records import WorkspaceProblem


def install_current_session_authority(identity):
    """Bind one workbench's lifecycle repository to its active identity service."""
    workspace = identity.store
    workspace._case_identity_service = identity
    workspace._case_session_authority = lambda actor_id, session_id, administrator_override: (
        require_current_session_authority(
            workspace, actor_id, session_id, administrator_override))


def require_current_session_authority(workspace, actor_id, session_id, administrator_override):
    """Reject stale/provider-mismatched authority without changing identity state."""
    try:
        principal = workspace.get_principal(actor_id)
        session = workspace.session_by_id(session_id)
        roles = json.loads(session.application_roles)
        now = workspace.current_time()
        invalid = (
            not principal.active or session.principal_id != actor_id
            or session.revoked_at is not None
            or datetime.fromisoformat(session.idle_expires_at.replace('Z', '+00:00')) <= now
            or datetime.fromisoformat(session.absolute_expires_at.replace('Z', '+00:00')) <= now
            or not isinstance(roles, list)
            or any(role != 'administrator' for role in roles)
            or len(roles) != len(set(roles)))
    except (KeyError, TypeError, ValueError):
        raise WorkspaceProblem('Sign in again before changing matter closure.') from None
    if invalid:
        raise WorkspaceProblem('Sign in again before changing matter closure.')
    if administrator_override and 'administrator' not in roles:
        raise WorkspaceProblem('Current administrator authority is required.')

    identity = getattr(workspace, '_case_identity_service', None)
    if identity is None:
        # Repository-only tests explicitly use persisted synthetic sessions.
        if principal.provider == 'test' and session.auth_method == 'test':
            return
        raise WorkspaceProblem('Current identity-provider authority is unavailable.')

    mode = identity.auth_mode
    if mode == 'local':
        settings = identity.local_settings
        if (settings is None or principal.provider != settings.provider_key
                or session.auth_method != 'local'):
            raise WorkspaceProblem('Sign in again with the current identity provider.')
        try:
            account = settings.account(principal.provider_subject)
        except (OSError, RuntimeError, ValueError):
            account = None
        if account is None or not account.enabled:
            raise WorkspaceProblem('This identity is not active.')
        if administrator_override and 'administrator' not in account.roles:
            raise WorkspaceProblem('Current administrator authority is required.')
        with workspace._lock:
            saved = workspace.connection.execute(
                'SELECT binding_digest FROM workbench_case_session_binding WHERE session_id=?',
                (session_id,)).fetchone()
        current_binding = hashlib.sha256(
            identity._local_session_binding(account).encode('ascii')).hexdigest()
        if saved is None or not hmac.compare_digest(saved['binding_digest'], current_binding):
            raise WorkspaceProblem('Sign in again before changing matter closure.')
        return

    if mode == 'kerberos':
        settings = identity.kerberos_settings
        if (settings is None or principal.provider != settings.provider_key
                or session.auth_method != 'spnego'):
            raise WorkspaceProblem('Sign in again with the current identity provider.')
        # Authentication already belongs to this persisted SPNEGO session.
        # Reuse the provider's exact current admission/group policy, including
        # its explicit-principal fallback when group lookup is unavailable.
        _, current_roles = identity._verified_kerberos_authorization(
            principal.provider_subject, settings.proxy_secret)
        if administrator_override and 'administrator' not in current_roles:
            raise WorkspaceProblem('Current administrator authority is required.')
        return

    if mode in {'preview', 'test'}:
        preview_principal = (
            principal.provider == 'preview'
            and principal.provider_subject in identity._preview_by_subject)
        synthetic_principal = mode == 'test' and principal.provider == 'test'
        if (session.auth_method != mode or not (preview_principal or synthetic_principal)):
            raise WorkspaceProblem('Sign in again with the current identity provider.')
        return

    if mode == 'oidc':
        settings = identity.oidc_settings
        if (settings is None or principal.provider != settings.provider_key
                or session.auth_method != 'oidc'):
            raise WorkspaceProblem('Sign in again with the current identity provider.')
        if administrator_override:
            raise WorkspaceProblem(
                'Current organization administrator authority cannot be verified for matter closure.')
        return

    raise WorkspaceProblem('Current identity-provider authority is unavailable.')
