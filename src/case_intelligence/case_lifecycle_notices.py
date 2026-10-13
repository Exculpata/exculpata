"""Authorized closure notices; presentation is not owner acknowledgement."""
from __future__ import annotations

from datetime import datetime


def can_manage_closure(workspace, matter_id, context):
    """Navigation follows the same live owner/admin boundary as exit actions."""
    with workspace._lock:
        try:
            workspace.case_lifecycle._owner(matter_id, context.principal_id, context.is_administrator)
            return True
        except KeyError:
            return False


def closing_notices(workspace, context, request_id):
    """Return current notices and audit their first presentation to each viewer.

    Keep live membership, exit selection and deduplication in one transaction.
    The audit contains role and opaque identities only, never matter names,
    source content or a claim that the viewer read or accepted the notice.
    """
    lifecycle = workspace.case_lifecycle
    notices = []
    with lifecycle._transaction():
        for matter in lifecycle.closing_matters(
            context.principal_id, administrator_override=context.is_administrator
        ):
            current = lifecycle.current_exit(matter.matter_id)
            if current is None or current.state != 'closing':
                continue
            can_manage = can_manage_closure(workspace, matter.matter_id, context)
            role = ('owner' if can_manage and matter.owner_id == context.principal_id else
                    'administrator' if context.is_administrator else 'member')
            notices.append(dict(
                matter=matter, exit_id=current.exit_id, requested_at=current.requested_at,
                eligible_after=current.eligible_after, can_manage=can_manage,
                administrator_requested=current.requested_by != matter.owner_id,
                window_elapsed=workspace.current_time() >= datetime.fromisoformat(
                    current.eligible_after.replace('Z', '+00:00')),
            ))
            seen = workspace.connection.execute(
                "SELECT 1 FROM workbench_audit_event WHERE matter_id=? AND actor_principal_id=? "
                "AND action='matter.exit_notice' AND object_id=? AND outcome='success' LIMIT 1",
                (matter.matter_id, context.principal_id, current.exit_id),
            ).fetchone()
            if seen is None:
                workspace._append_audit_event_locked(
                    actor_principal_id=context.principal_id,
                    session_id=context.session.session_id if context.session is not None else None,
                    matter_id=matter.matter_id, request_id=request_id,
                    action='matter.exit_notice', outcome='success', object_type='case_exit',
                    object_id=current.exit_id, details={'role': role},
                )
    return tuple(notices)
