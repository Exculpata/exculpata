"""Synthetic repository proof for delivery coverage and lifecycle invalidation."""
import sqlite3
import threading

import pytest

from case_intelligence.workspace_store import WorkspaceProblem, WorkspaceStore
from tests.test_case_lifecycle import setup, session, OWNER, ARCHIVE, DIGEST, EXPORT  # noqa: F401


def pending(store, matter):
    sess = session(store)
    current = store.case_lifecycle.begin_exit(matter.slug, OWNER, session_id=sess.session_id)
    current = store.case_lifecycle.register_export(matter.matter_id, OWNER, current.exit_id,
        EXPORT, ARCHIVE, DIGEST, session_id=sess.session_id)
    return current, sess


def attempt(store, current, sess, start, end, *, size=100):
    return store.case_lifecycle.downloads.begin(current.matter_id, OWNER, sess.session_id,
        current.export_id, current.archive_digest, size, start, end)


def test_interval_union_requires_every_byte_and_audits_once(setup):
    store, matter, clock = setup
    current, sess = pending(store, matter)
    downloads = store.case_lifecycle.downloads
    for start, end in [(50, 100), (0, 20), (0, 20), (10, 49), (51, 80)]:
        assert not downloads.complete(attempt(store, current, sess, start, end))
    assert downloads.receipt(current, OWNER) is None
    clock.advance(days=7)
    with pytest.raises(WorkspaceProblem, match='complete current archive'):
        store.case_lifecycle.issue_confirmation(matter.slug, OWNER, sess.session_id, EXPORT)
    assert downloads.complete(attempt(store, current, sess, 49, 50))
    assert downloads.complete(attempt(store, current, sess, 0, 100))
    assert downloads.receipt(current, OWNER)['intervals_json'] == '[[0, 100]]'
    assert len([e for e in store.audit_events(matter.matter_id) if e.action == 'matter.exit_download']) == 1
    # Ordinary reauthentication does not erase completed delivery by this actor.
    assert store.case_lifecycle.issue_confirmation(matter.slug, OWNER, session(store).session_id, EXPORT)


@pytest.mark.parametrize('transition', ['replace', 'cancel', 'restart'])
def test_late_completion_cannot_repopulate_invalidated_attempt(setup, transition):
    store, matter, _ = setup
    current, sess = pending(store, matter)
    downloads = store.case_lifecycle.downloads
    value = attempt(store, current, sess, 0, 100)
    if transition == 'replace':
        store.case_lifecycle.register_export(matter.matter_id, OWNER, current.exit_id,
            'full-export-' + 'd' * 32, 'e' * 64, DIGEST, session_id=sess.session_id)
    elif transition == 'cancel':
        store.case_lifecycle.cancel_exit(matter.slug, OWNER, current.exit_id, session_id=sess.session_id)
    else:
        store.case_lifecycle.discard_confirmations_on_startup()
    assert downloads.complete(value) is False
    assert downloads.receipt(current, OWNER) is None


def test_restart_retains_completed_observation_but_invalidates_pending_capabilities(setup):
    store, matter, clock = setup
    current, sess = pending(store, matter)
    assert store.case_lifecycle.downloads.complete(attempt(store, current, sess, 0, 100))
    clock.advance(days=7)
    store.case_lifecycle.issue_confirmation(matter.slug, OWNER, sess.session_id, EXPORT)
    late = attempt(store, current, sess, 0, 100)
    store.case_lifecycle.discard_confirmations_on_startup()
    assert store.case_lifecycle.downloads.receipt(current, OWNER) is not None
    assert not store.case_lifecycle.downloads.complete(late)
    assert store.connection.execute('SELECT COUNT(*) FROM workbench_case_confirmation').fetchone()[0] == 0


def test_concurrent_overlapping_completions_merge_without_lost_coverage(setup):
    store, matter, clock = setup
    current, sess = pending(store, matter)
    values = [attempt(store, current, sess, 0, 60), attempt(store, current, sess, 40, 100)]
    other = WorkspaceStore(store.path, clock=clock)
    barrier = threading.Barrier(2)
    errors = []
    def complete(workspace, value):
        try:
            barrier.wait(timeout=5)
            workspace.case_lifecycle.downloads.complete(value)
        except BaseException as exc:
            errors.append(exc)
    threads = [threading.Thread(target=complete, args=args) for args in ((store, values[0]), (other, values[1]))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
        assert not thread.is_alive()
    assert errors == []
    assert store.case_lifecycle.downloads.receipt(current, OWNER)['intervals_json'] == '[[0, 100]]'
    other.close()


def test_audit_commit_failure_cannot_create_delivery_authority(setup, monkeypatch):
    store, matter, _ = setup
    current, sess = pending(store, matter)
    value = attempt(store, current, sess, 0, 100)
    def fail(**kwargs):
        raise sqlite3.OperationalError('Synthetic audit failure')
    monkeypatch.setattr(store, '_append_audit_event_locked', fail)
    with pytest.raises(sqlite3.OperationalError):
        store.case_lifecycle.downloads.complete(value)
    assert store.case_lifecycle.downloads.receipt(current, OWNER) is None
    assert not store.connection.in_transaction


def test_actor_size_and_revoked_session_cannot_reuse_delivery(setup):
    store, matter, _ = setup
    current, sess = pending(store, matter)
    value = attempt(store, current, sess, 0, 50)
    assert not store.case_lifecycle.downloads.complete(value)
    changed_size = attempt(store, current, sess, 50, 101, size=101)
    with pytest.raises(WorkspaceProblem, match='size changed'):
        store.case_lifecycle.downloads.complete(changed_size)
    value = attempt(store, current, sess, 0, 100)
    store.revoke_session(sess.session_id)
    with pytest.raises(WorkspaceProblem, match='Sign in'):
        store.case_lifecycle.downloads.complete(value)
    assert store.case_lifecycle.downloads.receipt(current, OWNER) is None
