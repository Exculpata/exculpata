"""Application boundaries for ongoing matters and explicitly confirmed exits."""
from __future__ import annotations

import os
import re
import threading
from contextlib import contextmanager
from datetime import timedelta

from .pilot_uploads import PilotStore, UploadProblem, securely_delete_owned_tree
from .workspace_store import WorkspaceProblem
from .source_cache import source_cache_guard


@contextmanager
def archive_stage_reservation(bench, key, amount):
    """Share protected disk capacity with upload and playback admission."""
    amount = max(int(amount), 1)
    with bench._storage_reservation_lock:
        if key in bench._preservation_reservations:
            raise WorkspaceProblem('An archive staging reservation already exists.')
        bench.storage.ensure_capacity(bench.workspace.pending_upload_bytes()
            + bench._playback_reserved_bytes() + amount)
        bench._preservation_reservations[key] = amount
    try:
        yield
    finally:
        with bench._storage_reservation_lock:
            bench._preservation_reservations.pop(key, None)


def source_lock(bench, matter_id):
    return bench._matter_source_locks.setdefault(matter_id, threading.RLock())


def ensure_writable(bench, matter_id):
    if bench.workspace.matter_lifecycle(matter_id).state != 'active':
        raise UploadProblem('This matter is closing and does not accept changes. Reopen it first.', 409)


def source_store(bench, matter, *, archive=False):
    if not re.fullmatch(r'ci-matter-[0-9a-f]{32}', matter.matter_id):
        raise RuntimeError('Stored matter identity is invalid.')
    with source_lock(bench, matter.matter_id), source_cache_guard(bench):
        state = bench.workspace.matter_lifecycle(matter.matter_id).state
        if state not in {'active', 'closing', 'purge_failed'}:
            raise UploadProblem('This matter cannot open its source storage.', 409)
        existing = bench._stores.get(matter.matter_id)
        if existing is not None and not archive:
            if not existing._read_only or state != 'active':
                return existing
            existing.close()
            bench._stores.pop(matter.matter_id)
        frozen = archive or state != 'active'
        store = PilotStore(
            bench.storage.matters / matter.matter_id / 'sources',
            document_file_limit=bench.storage_policy.document_file_bytes,
            media_file_limit=bench.storage_policy.media_file_bytes,
            upload_session_limit=bench.storage_policy.upload_session_bytes,
            on_change=None if frozen else lambda change: bench._apply_source_store_change(matter, change),
            mutation_lock=source_lock(bench, matter.matter_id),
            malware_scanner=bench.malware_scanner,
            malware_scan_mode=bench.malware_scan_mode,
            read_only=frozen,
            write_check=lambda: ensure_writable(bench, matter.matter_id),
        )
        if archive:
            return store
        bench._stores[matter.matter_id] = store
        if not frozen:
            if store.documents:
                bench.workspace.reconcile_source_organizations(matter.matter_id, tuple(
                    (doc.document_id, doc.origin, doc.relative_path or doc.display_name)
                    for doc in store.documents.values()))
            bench._sync_source_catalog(matter, tuple(store.documents.values()))
        return store


def retention_projection(bench, matter):
    policy = bench.workspace.case_lifecycle.policy(matter.matter_id)
    lifecycle = bench.workspace.matter_lifecycle(matter.matter_id)
    now = bench.workspace.current_time()
    next_at = bench._parse_utc(policy.next_check_in_at) if policy.next_check_in_at else None
    remaining = next_at - now if next_at else None
    due = remaining is not None and remaining <= timedelta(0)
    review = policy.mode == 'legacy_review'
    soon = not review and remaining is not None and timedelta(0) < remaining <= timedelta(days=14)
    next_week = soon and remaining <= timedelta(days=7)
    ownership_review = not review and remaining is not None and remaining <= -timedelta(days=30)
    status = ('review_required' if review else 'ownership_review' if ownership_review else
              'check_in_due' if due else 'check_in_next_week' if next_week else
              'check_in_soon' if soon else 'ongoing')
    label = ('Unscheduled · no automatic deletion' if policy.mode == 'unscheduled' else
             'Review the previous schedule' if review else
             'Ownership review needed · check-in 30 days overdue' if ownership_review else
             '90-day check-in due' if due else 'Check-in due within 7 days' if next_week else
             'Check-in due within 14 days' if soon else 'Ongoing until explicitly closed')
    detail = ('Previous review and deletion dates are paused. Review this policy to keep the matter ongoing.'
              if review else 'The owner or administrator must review ownership and arrange a check-in or explicit exit. Access continues; deletion is never automatic.'
              if ownership_review else 'Check in to confirm continued use; an overdue check-in never deletes a matter.'
              if due else 'Check in to confirm continued use; deletion is never automatic.'
              if next_at else 'No automatic deletion is scheduled.')
    attention = review or due or soon
    if lifecycle.state != 'active':
        label, detail = 'Closure in progress', 'The matter is frozen. Review its export and exit status.'
    return dict(record=policy, status=status, tone='attention' if attention else 'quiet',
                label=label, short_label='Review policy' if review else 'Check in' if due or soon else 'Ongoing',
                detail=detail, show_warning=attention,
                next_check_in_on=next_at.date().isoformat() if next_at else '',
                next_check_in_at=policy.next_check_in_at,
                legacy_expires_at=policy.legacy_expires_at, legacy_purge_after=policy.legacy_purge_after,
                expires_on='', purge_on='', days_remaining=None,
                minimum_date=(now + timedelta(days=1)).date().isoformat(),
                maximum_date=(now + timedelta(days=365)).date().isoformat())


def source_count(bench, matter, lifecycle):
    if lifecycle.state not in {'active', 'closing'}:
        return lifecycle.source_count
    store = bench.archive_source_store(matter)
    try:
        return store.document_count()
    finally:
        store.close()


def idle(bench, matter_id):
    if any(bench.matter_active_work_counts(matter_id).values()):
        raise WorkspaceProblem('Wait for current uploads, source and media processing, transcript overviews, analysis, browser playback preparation, work-product downloads, answers, investigations, and every-source checks to finish before closing or deleting this matter.')


def begin_purge(bench, slug, actor_id, confirmed_name, *, administrator_override=False, refresh_authority=None, **confirmation):
    from .matter_archive import matter_snapshot_digest
    matter, lifecycle = bench.matter_for_closure(slug, actor_id, administrator_override=administrator_override)
    with source_lock(bench, matter.matter_id):
        idle(bench, matter.matter_id)
        current = bench.workspace.case_lifecycle.current_exit(matter.matter_id)
        if current is None:
            raise WorkspaceProblem('Start an explicit exit and choose its closure path before deletion.')
        if lifecycle.state == 'purge_failed' and current.state == 'confirmed':
            digest = current.snapshot_digest
            count = lifecycle.source_count
        else:
            # Restored receipts alone never authorize a first destructive claim.
            if current.mode == 'export_exit':
                verified_archive(bench, matter, current)
            store = bench.archive_source_store(matter)
            try:
                digest = matter_snapshot_digest(bench, matter, store) if current.mode == 'export_exit' else None
                count = store.document_count()
            finally:
                store.close()
        if refresh_authority is not None:
            refresh_authority()
        return bench.workspace.case_lifecycle.claim_purge(
            slug, actor_id, confirmed_name, source_count=count,
            current_snapshot_digest=digest, administrator_override=administrator_override, **confirmation)


def archive_directory(bench, matter_id):
    bench._validate_clip_export_matter_id(matter_id)
    root = bench.storage.root / 'preservation-exports'
    target = root / matter_id
    if any(path.is_symlink() for path in (target, root, *root.parents)):
        raise WorkspaceProblem('Preservation storage is unsafe.')
    return target


def archive_path(bench, matter, current):
    if not re.fullmatch(r'matter-export-[0-9a-f]{32}', current.export_id or ''):
        raise WorkspaceProblem('A verified full export is required.')
    directory = archive_directory(bench, matter.matter_id) / current.export_id
    path = directory / 'preservation.zip'
    if directory.is_symlink() or path.is_symlink() or not path.is_file():
        raise WorkspaceProblem('The saved full export is unavailable. Create and verify a new export before deletion.')
    return path


def verified_archive(bench, matter, current):
    from .matter_archive_verifier import verify_matter_archive
    path = archive_path(bench, matter, current)
    try:
        manifest = verify_matter_archive(path, expected_sha256=current.archive_digest,
                                        maximum_bytes=bench.storage_policy.matter_quota_bytes)
    except (OSError, ValueError, KeyError, RuntimeError) as exc:
        raise WorkspaceProblem('The saved full export could not be verified. Create a new export before deletion.') from exc
    if (manifest.get('matter_id') != matter.matter_id or manifest.get('exit_id') != current.exit_id
            or manifest.get('export_id') != current.export_id or manifest.get('snapshot_digest') != current.snapshot_digest):
        raise WorkspaceProblem('The saved full export does not match this exit.')
    return path


def install_archive(bench, matter, artifact):
    directory = archive_directory(bench, matter.matter_id)
    directory.mkdir(mode=0o700, exist_ok=True)
    target = directory / artifact.export_id
    if target.exists() or target.is_symlink():
        raise WorkspaceProblem('The export destination is already occupied.')
    os.replace(artifact.cleanup_directory, target)
    PilotStore._fsync_directory(directory)
    return target / 'preservation.zip'


def cleanup_archives(bench, matter_id, *, keep=None):
    directory = archive_directory(bench, matter_id)
    if not directory.exists():
        return
    if not directory.is_dir():
        raise WorkspaceProblem('Preservation storage is unsafe.')
    for child in directory.iterdir():
        if child.name == keep:
            continue
        if not re.fullmatch(r'(?:matter-export|building)-[0-9a-f]{32}', child.name):
            raise WorkspaceProblem('Preservation storage contains an unexpected component.')
        securely_delete_owned_tree(child, allowed_parent=directory)
    if keep is None:
        directory.rmdir()
        PilotStore._fsync_directory(directory.parent)


def quarantine_sources(bench, matter, purge_id, quarantine):
    """Move an authorized frozen tree without invoking startup reconciliation."""
    with source_lock(bench, matter.matter_id), source_cache_guard(bench):
        bench.workspace.case_lifecycle.validate_purge(matter.matter_id, purge_id)
        root = bench.storage.matters / matter.matter_id / 'sources'
        if root.is_symlink() or not root.is_dir() or quarantine.is_symlink() or quarantine.exists():
            raise RuntimeError('Matter purge storage is unsafe.')
        if quarantine.parent.is_symlink() or not quarantine.parent.is_dir():
            raise RuntimeError('Matter purge boundary is unsafe.')
        existing = bench._stores.pop(matter.matter_id, None)
        if existing is not None:
            existing.close()
        os.replace(root, quarantine)
        PilotStore._fsync_directory(quarantine.parent)
        PilotStore._fsync_directory(root.parent)
