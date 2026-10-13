"""Repository-only synthetic exit fixtures; archive bytes are tested separately.

The fake digests model a successful trusted full-archive service registration.
Never use this helper for HTTP or filesystem export completeness acceptance.
"""
from datetime import datetime, timedelta
import hashlib
import uuid


def lifecycle_session(store, actor_id, *, administrator_override=False):
    """Persist an explicitly synthetic service session for repository fixtures."""
    end = store._timestamp(store.current_time() + timedelta(days=30))
    identity = getattr(store, '_case_identity_service', None)
    method = {'kerberos': 'spnego', 'preview': 'preview'}.get(identity.auth_mode, 'test') if identity else 'test'
    return store.create_session(actor_id, hashlib.sha256(uuid.uuid4().bytes).hexdigest(),
        method, end, end, frozenset({'administrator'}) if administrator_override else frozenset())


def record_synthetic_delivery(store, current, actor_id, session_id, *, administrator_override=False):
    """Repository model only; HTTP/transport acceptance uses actual archive bytes."""
    attempt = store.case_lifecycle.downloads.begin(current.matter_id, actor_id, session_id,
        current.export_id, current.archive_digest, 100, 0, 100,
        administrator_override=administrator_override)
    assert store.case_lifecycle.downloads.complete(attempt)


def prepare_purge_confirmation(store, matter, actor_id, *, administrator_override=False):
    admission = lifecycle_session(store, actor_id, administrator_override=administrator_override)
    current = store.case_lifecycle.current_exit(matter.matter_id)
    if current is None:
        current = store.case_lifecycle.begin_exit(matter.slug, actor_id, session_id=admission.session_id, administrator_override=administrator_override)
    if current.state == 'closing' and current.export_id is None:
        current = store.case_lifecycle.register_export(
            matter.matter_id, actor_id, current.exit_id, 'full-export-' + uuid.uuid4().hex,
            'a' * 64, 'b' * 64, session_id=admission.session_id, administrator_override=administrator_override)
    eligible = datetime.fromisoformat(current.eligible_after.replace('Z', '+00:00'))
    now = max(store.current_time(), eligible)
    store._clock = lambda: now
    expires = store._timestamp(now + timedelta(hours=1))
    session = store.create_session(actor_id, hashlib.sha256(uuid.uuid4().bytes).hexdigest(),
                                   'test', expires, expires,
                                   frozenset({'administrator'}) if administrator_override else frozenset())
    if current.state == 'closing':
        record_synthetic_delivery(store, current, actor_id, session.session_id,
                                  administrator_override=administrator_override)
    nonce = store.case_lifecycle.issue_confirmation(
        matter.slug, actor_id, session.session_id, current.export_id, administrator_override=administrator_override)
    return dict(token=nonce.token, session_id=session.session_id, export_id=current.export_id,
                export_acknowledged=True, current_snapshot_digest=current.snapshot_digest)


def claim_synthetic_exit(store, matter, actor_id, *, source_count=0):
    return store.begin_matter_purge(matter.slug, actor_id, matter.display_name,
        source_count=source_count, **prepare_purge_confirmation(store, matter, actor_id))
