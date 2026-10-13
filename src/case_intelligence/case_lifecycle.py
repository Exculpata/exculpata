"""Explicit matter exits. This repository never removes files or exports bytes.

The application must hold its source mutation lock while exporting, recomputing
snapshot digests and claiming a purge. SQLite serializes durable work admission
with these transitions. Export digests are accepted only from the verified full
archive service, never from form inputs. A date alone never authorizes deletion.
"""
from __future__ import annotations

import hashlib
import json
import re
import secrets
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta

from .store_records import WorkspaceProblem

_DIGEST = re.compile(r'^[0-9a-f]{64}$')
_EXPORT_ID = re.compile(r'^[a-z][a-z0-9-]{7,99}$')
CHECK_IN_DAYS = 90
CANCELLATION_DAYS = 7
CONFIRMATION_MINUTES = 10


def _instant(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00'))


@dataclass(frozen=True)
class CasePolicy:
    matter_id: str
    policy_version: int
    mode: str
    next_check_in_at: str | None
    last_checked_at: str | None
    selected_by: str | None
    created_at: str
    updated_at: str
    legacy_expires_at: str | None
    legacy_purge_after: str | None


@dataclass(frozen=True)
class CaseExit:
    exit_id: str
    matter_id: str
    state: str
    requested_by: str
    requested_at: str
    eligible_after: str
    export_id: str | None
    archive_digest: str | None
    snapshot_digest: str | None
    exported_by: str | None
    exported_at: str | None
    confirmed_by: str | None
    confirmed_at: str | None
    purge_id: str | None
    updated_at: str


@dataclass(frozen=True)
class PurgeConfirmation:
    token: str
    expires_at: str


class CaseLifecycleRepository:
    def __init__(self, workspace):
        self.workspace = workspace
        self.connection = workspace.connection

    def discard_confirmations_on_startup(self):
        """A restarted or restored application requires a newly displayed form.

        Keep durable exits, export receipts and confirmed purge identities. Only
        unclaimed form capabilities belong to the previous application lifetime.
        """
        with self._transaction():
            self.connection.execute('DELETE FROM workbench_case_confirmation')

    @contextmanager
    def _transaction(self):
        with self.workspace._lock, self.connection:
            self.connection.execute('BEGIN IMMEDIATE')
            yield

    def _actor(self, actor_id):
        actor = self.workspace.get_principal(actor_id)
        if not actor.active or not self.workspace.principal_enabled(actor.provider, actor.provider_subject):
            raise WorkspaceProblem('This identity is not active.')
        return actor

    def _owner(self, matter_id, actor_id, administrator_override=False):
        self._actor(actor_id)
        row = self.connection.execute(
            'SELECT m.* FROM workbench_matter m WHERE m.matter_id=?', (matter_id,)).fetchone()
        if row is None:
            raise KeyError(matter_id)
        if not administrator_override:
            grant = self.connection.execute(
                "SELECT 1 FROM workbench_matter_membership WHERE matter_id=? AND principal_id=? "
                "AND role='owner' AND state='active'", (matter_id, actor_id)).fetchone()
            if row['owner_id'] != actor_id or grant is None:
                raise KeyError(matter_id)
        return self.workspace._matter(row)

    def _by_slug(self, slug, actor_id, administrator_override=False):
        row = self.connection.execute(
            'SELECT matter_id FROM workbench_matter WHERE slug=?', (slug,)).fetchone()
        if row is None:
            raise KeyError(slug)
        return self._owner(row['matter_id'], actor_id, administrator_override)

    def _session(self, actor_id, session_id, administrator_override=False):
        self._actor(actor_id)
        try:
            session = self.workspace.session_by_id(session_id)
        except (KeyError, TypeError):
            raise WorkspaceProblem('Sign in again before confirming deletion.') from None
        now = self.workspace.current_time()
        if (session.principal_id != actor_id or session.revoked_at is not None
                or _instant(session.idle_expires_at) <= now or _instant(session.absolute_expires_at) <= now):
            raise WorkspaceProblem('Sign in again before confirming deletion.')
        if administrator_override and 'administrator' not in json.loads(session.application_roles):
            raise WorkspaceProblem('Current administrator authority is required.')

    def _idle(self, matter_id):
        if any(self.workspace._active_matter_work_counts_locked(matter_id).values()):
            raise WorkspaceProblem(
                'Wait for current uploads, source and media processing, transcript overviews, '
                'analysis, answers, investigations, reports, and every-source checks to finish '
                'before closing or deleting this matter.')
        if self.workspace._text_review_export_leases.get(matter_id, 0):
            raise WorkspaceProblem('Wait for the current export to finish.')

    def _audit(self, action, matter_id, actor_id, object_id=None, session_id=None, *, administrator_override=False):
        self.workspace._append_audit_event_locked(
            actor_principal_id=actor_id, session_id=session_id, matter_id=matter_id,
            request_id='case-lifecycle', action=action, outcome='success',
            object_type='case_exit' if object_id else 'matter', object_id=object_id or matter_id,
            details={'role': 'administrator' if administrator_override else 'owner'})

    def policy(self, matter_id) -> CasePolicy:
        with self.workspace._lock:
            row = self.connection.execute(
                'SELECT p.*,r.expires_at AS legacy_expires_at,r.purge_after AS legacy_purge_after '
                'FROM workbench_case_policy p LEFT JOIN workbench_matter_retention r '
                'ON r.matter_id=p.matter_id WHERE p.matter_id=?', (matter_id,)).fetchone()
            if row is None:
                raise KeyError(matter_id)
            return CasePolicy(**dict(row))

    def current_exit(self, matter_id) -> CaseExit | None:
        with self.workspace._lock:
            row = self.connection.execute(
                "SELECT * FROM workbench_case_exit WHERE matter_id=? AND state IN ('closing','confirmed')",
                (matter_id,)).fetchone()
            return CaseExit(**dict(row)) if row else None

    def read_matter(self, slug, actor_id, *, administrator_override=False):
        """Explicit read-only access; does not grant membership or write authority."""
        with self.workspace._lock:
            self._actor(actor_id)
            row = self.connection.execute(
                "SELECT m.* FROM workbench_matter m JOIN workbench_matter_lifecycle l "
                "ON l.matter_id=m.matter_id WHERE m.slug=? AND l.state<>'deleted'", (slug,)).fetchone()
            if row is None:
                raise KeyError(slug)
            state = self.workspace.matter_lifecycle(row['matter_id']).state
            if state not in {'active', 'closing'}:
                return self._owner(row['matter_id'], actor_id, administrator_override)
            if not administrator_override:
                direct = self.connection.execute(
                    "SELECT 1 FROM workbench_matter_membership WHERE matter_id=? "
                    "AND principal_id=? AND state='active'", (row['matter_id'], actor_id)).fetchone()
                grouped = self.connection.execute(
                    'SELECT 1 FROM workbench_matter_group_grant mg JOIN workbench_team_group_member gm '
                    'ON gm.group_id=mg.group_id WHERE mg.matter_id=? AND gm.principal_id=?',
                    (row['matter_id'], actor_id)).fetchone()
                if not direct and not grouped:
                    raise KeyError(slug)
            return self.workspace._matter(row)

    def closing_matters(self, actor_id, *, administrator_override=False):
        with self.workspace._lock:
            self._actor(actor_id)
            rows = self.connection.execute(
                "SELECT m.slug FROM workbench_matter m JOIN workbench_matter_lifecycle l "
                "ON l.matter_id=m.matter_id WHERE l.state='closing' ORDER BY m.updated_at DESC"
            ).fetchall()
            found = []
            for row in rows:
                try:
                    found.append(self.read_matter(row['slug'], actor_id,
                                                  administrator_override=administrator_override))
                except KeyError:
                    continue
            return tuple(found)

    def check_in(self, matter_id, actor_id, *, administrator_override=False) -> CasePolicy:
        with self._transaction():
            self._owner(matter_id, actor_id, administrator_override)
            if self.workspace.matter_lifecycle(matter_id).state != 'active':
                raise WorkspaceProblem('Reopen this matter before checking in.')
            now = self.workspace._now()
            next_check = self.workspace._timestamp(
                self.workspace.current_time() + timedelta(days=CHECK_IN_DAYS))
            changed = self.connection.execute(
                "UPDATE workbench_case_policy SET mode='ongoing',next_check_in_at=?,last_checked_at=?,"
                'selected_by=?,updated_at=? WHERE matter_id=?',
                (next_check, now, actor_id, now, matter_id)).rowcount
            if changed != 1:
                raise KeyError(matter_id)
            self._audit('matter.check_in', matter_id, actor_id, administrator_override=administrator_override)
            return self.policy(matter_id)

    def begin_exit(self, slug, actor_id, *, administrator_override=False) -> CaseExit:
        with self._transaction():
            matter = self._by_slug(slug, actor_id, administrator_override)
            lifecycle = self.workspace.matter_lifecycle(matter.matter_id)
            current = self.current_exit(matter.matter_id)
            if lifecycle.state == 'closing' and current:
                return current
            if lifecycle.state != 'active':
                raise WorkspaceProblem('This matter requires recovery before starting an exit.')
            self._idle(matter.matter_id)
            now = self.workspace._now()
            eligible = self.workspace._timestamp(
                self.workspace.current_time() + timedelta(days=CANCELLATION_DAYS))
            exit_id = 'case-exit-' + uuid.uuid4().hex
            self.connection.execute(
                "INSERT INTO workbench_case_exit(exit_id,matter_id,state,requested_by,requested_at,"
                "eligible_after,updated_at) VALUES (?,?,'closing',?,?,?,?)",
                (exit_id, matter.matter_id, actor_id, now, eligible, now))
            self.connection.execute(
                "UPDATE workbench_matter_lifecycle SET state='closing',updated_at=? "
                "WHERE matter_id=? AND state='active'", (now, matter.matter_id))
            self._audit('matter.exit_begin', matter.matter_id, actor_id, exit_id, administrator_override=administrator_override)
            return self.current_exit(matter.matter_id)

    def cancel_exit(self, slug, actor_id, exit_id, *, administrator_override=False):
        with self._transaction():
            matter = self._by_slug(slug, actor_id, administrator_override)
            current = self.current_exit(matter.matter_id)
            lifecycle = self.workspace.matter_lifecycle(matter.matter_id)
            cancelled = self.connection.execute(
                "SELECT 1 FROM workbench_case_exit WHERE matter_id=? AND exit_id=? AND state='cancelled'",
                (matter.matter_id, exit_id)).fetchone()
            if lifecycle.state == 'active' and current is None and cancelled:
                return self.policy(matter.matter_id)
            if (current is None or current.exit_id != exit_id or current.state != 'closing'
                    or lifecycle.state != 'closing'):
                raise WorkspaceProblem('This exit cannot be cancelled; refresh its current status.')
            now = self.workspace._now()
            self.connection.execute('DELETE FROM workbench_case_confirmation WHERE exit_id=?', (exit_id,))
            self.connection.execute(
                "UPDATE workbench_case_exit SET state='cancelled',export_id=NULL,archive_digest=NULL,"
                'snapshot_digest=NULL,exported_by=NULL,exported_at=NULL,updated_at=? WHERE exit_id=?',
                (now, exit_id))
            self.connection.execute(
                "UPDATE workbench_matter_lifecycle SET state='active',updated_at=? WHERE matter_id=?",
                (now, matter.matter_id))
            self._audit('matter.exit_cancel', matter.matter_id, actor_id, exit_id, administrator_override=administrator_override)
            return self.policy(matter.matter_id)

    def register_export(self, matter_id, actor_id, exit_id, export_id, archive_digest,
                        snapshot_digest, *, administrator_override=False) -> CaseExit:
        """Called only after the full archive service has verified every entry."""
        if (not _EXPORT_ID.fullmatch(export_id) or not _DIGEST.fullmatch(archive_digest)
                or not _DIGEST.fullmatch(snapshot_digest)):
            raise ValueError('Invalid verified export identity.')
        with self._transaction():
            self._owner(matter_id, actor_id, administrator_override)
            current = self.current_exit(matter_id)
            if (current is None or current.exit_id != exit_id or current.state != 'closing'
                    or self.workspace.matter_lifecycle(matter_id).state != 'closing'):
                raise WorkspaceProblem('The exit changed; create a new full export.')
            self._idle(matter_id)
            now = self.workspace._now()
            self.connection.execute('DELETE FROM workbench_case_confirmation WHERE exit_id=?', (exit_id,))
            self.connection.execute(
                'UPDATE workbench_case_exit SET export_id=?,archive_digest=?,snapshot_digest=?,exported_by=?, '
                'exported_at=?,updated_at=? WHERE exit_id=?',
                (export_id, archive_digest, snapshot_digest, actor_id, now, now, exit_id))
            self._audit('matter.exit_export', matter_id, actor_id, exit_id, administrator_override=administrator_override)
            return self.current_exit(matter_id)

    def _ready(self, matter_id, export_id):
        current = self.current_exit(matter_id)
        lifecycle = self.workspace.matter_lifecycle(matter_id)
        if current is None:
            raise WorkspaceProblem('Start an explicit exit before confirming deletion.')
        retry = lifecycle.state == 'purge_failed' and current.state == 'confirmed'
        if not retry and (lifecycle.state != 'closing' or current.state != 'closing'):
            raise WorkspaceProblem('This deletion cannot be confirmed in its current state.')
        if (not current.export_id or current.export_id != export_id
                or not current.archive_digest or not current.snapshot_digest):
            raise WorkspaceProblem('A verified full export for this exit is required.')
        if self.workspace.current_time() < _instant(current.eligible_after):
            raise WorkspaceProblem('The seven-day cancellation window has not ended.')
        return current, retry

    def issue_confirmation(self, slug, actor_id, session_id, export_id, *,
                           administrator_override=False) -> PurgeConfirmation:
        """Invoke only from the authenticated CSRF-protected confirmation action."""
        with self._transaction():
            matter = self._by_slug(slug, actor_id, administrator_override)
            self._session(actor_id, session_id, administrator_override)
            current, _ = self._ready(matter.matter_id, export_id)
            self._idle(matter.matter_id)
            token = secrets.token_urlsafe(32)
            now = self.workspace._now()
            expires = self.workspace._timestamp(
                self.workspace.current_time() + timedelta(minutes=CONFIRMATION_MINUTES))
            self.connection.execute('DELETE FROM workbench_case_confirmation WHERE exit_id=?', (current.exit_id,))
            self.connection.execute(
                'INSERT INTO workbench_case_confirmation VALUES (?,?,?,?,?,?,?,?,?,?)',
                (hashlib.sha256(token.encode()).hexdigest(), current.exit_id, actor_id, session_id,
                 hashlib.sha256(matter.display_name.encode()).hexdigest(), export_id, current.archive_digest, current.snapshot_digest, now, expires))
            self._audit('matter.exit_confirmation', matter.matter_id, actor_id, current.exit_id, session_id,
                        administrator_override=administrator_override)
            return PurgeConfirmation(token, expires)

    def claim_purge(self, slug, actor_id, confirmed_name, *, token, session_id, export_id,
                    export_acknowledged, source_count, current_snapshot_digest,
                    administrator_override=False):
        if type(source_count) is not int or source_count < 0:
            raise ValueError('Invalid purge source count.')
        if export_acknowledged is not True:
            raise WorkspaceProblem('Confirm that you downloaded and checked the full export.')
        if not isinstance(token, str) or not 32 <= len(token) <= 128:
            raise WorkspaceProblem('Request a fresh deletion confirmation.')
        with self._transaction():
            matter = self._by_slug(slug, actor_id, administrator_override)
            self._session(actor_id, session_id, administrator_override)
            if confirmed_name != matter.display_name:
                raise WorkspaceProblem('Enter the matter name exactly as shown to confirm deletion.')
            current, retry = self._ready(matter.matter_id, export_id)
            now = self.workspace._now()
            nonce = self.connection.execute(
                'SELECT * FROM workbench_case_confirmation WHERE token_digest=?',
                (hashlib.sha256(token.encode()).hexdigest(),)).fetchone()
            if (nonce is None or _instant(nonce['expires_at']) <= self.workspace.current_time() or nonce['exit_id'] != current.exit_id
                    or nonce['actor_id'] != actor_id or nonce['session_id'] != session_id
                    or nonce['name_digest'] != hashlib.sha256(matter.display_name.encode()).hexdigest()
                    or nonce['export_id'] != export_id or nonce['archive_digest'] != current.archive_digest
                    or nonce['snapshot_digest'] != current.snapshot_digest):
                raise WorkspaceProblem('Request a fresh deletion confirmation.')
            # Retries use the persisted pre-deletion snapshot: successful first
            # phases may already have removed bytes. They cannot restart an
            # unconfirmed legacy purge or substitute another archive.
            if current_snapshot_digest != current.snapshot_digest:
                raise WorkspaceProblem('Matter content changed; create and verify a new full export.')
            self._idle(matter.matter_id)
            purge_id = current.purge_id if retry else 'matter-purge-' + uuid.uuid4().hex
            if not purge_id:
                raise WorkspaceProblem('This interrupted deletion requires operator recovery.')
            counts = self.workspace.matter_content_counts(matter.matter_id)
            if retry:
                previous = self.workspace.matter_lifecycle(matter.matter_id)
                source_count = previous.source_count
                counts = {'conversations': previous.conversation_count, 'messages': previous.message_count}
            self.connection.execute(
                "UPDATE workbench_matter_lifecycle SET state='purging',purge_id=?,requested_by=?,"
                'requested_at=COALESCE(requested_at,?),finished_at=NULL,error_code=NULL,source_count=?, '
                'conversation_count=?,message_count=?,updated_at=? WHERE matter_id=?',
                (purge_id, actor_id, now, source_count, counts['conversations'], counts['messages'], now,
                 matter.matter_id))
            self.connection.execute(
                "UPDATE workbench_case_exit SET state='confirmed',confirmed_by=?,confirmed_at=COALESCE(confirmed_at,?),"
                'purge_id=?,updated_at=? WHERE exit_id=?', (actor_id, now, purge_id, now, current.exit_id))
            self.connection.execute('DELETE FROM workbench_case_confirmation WHERE exit_id=?', (current.exit_id,))
            self._audit('matter.purge_retry' if retry else 'matter.purge_confirm', matter.matter_id,
                        actor_id, current.exit_id, session_id, administrator_override=administrator_override)
            return matter, self.workspace.matter_lifecycle(matter.matter_id)

    @contextmanager
    def purge_cleanup(self, matter_id, purge_id):
        """Narrow content-write capability for validated control-store cleanup."""
        with self.workspace._lock:
            self.validate_purge(matter_id, purge_id)
            if self.workspace._case_cleanup_matter is not None:
                raise RuntimeError('A purge cleanup is already active.')
            self.workspace._case_cleanup_matter = matter_id
            try:
                yield
            finally:
                self.workspace._case_cleanup_matter = None

    def validate_purge(self, matter_id, purge_id) -> CaseExit:
        """Required immediately before filesystem deletion and control cleanup."""
        with self.workspace._lock:
            lifecycle = self.workspace.matter_lifecycle(matter_id)
            current = self.current_exit(matter_id)
            if (not purge_id or lifecycle.state != 'purging' or lifecycle.purge_id != purge_id
                    or current is None or current.state != 'confirmed' or current.purge_id != purge_id
                    or not current.confirmed_at or not current.confirmed_by or not current.export_id
                    or not current.archive_digest or not current.snapshot_digest
                    or _instant(current.confirmed_at) < _instant(current.eligible_after)):
                raise WorkspaceProblem('Deletion requires a confirmed current exit and verified full export.')
            return current
