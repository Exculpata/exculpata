"""Synthetic lifecycle transitions, authorization and recoverable exit gates."""

from tests.fixture_write import fixture_write

from case_intelligence.legacy_names import (
    LEGACY_STEM,
)

from tests.case_lifecycle_helpers import lifecycle_session
from datetime import datetime, timedelta, timezone
import hashlib
import sqlite3
import threading
import uuid

import pytest

from case_intelligence.workspace_store import WorkspaceProblem, WorkspaceStore

OWNER = 'principal-owner'
MEMBER = 'principal-member'
ADMIN = 'principal-admin'
NAME = 'Synthetic lifecycle matter'
DIGEST = 'a' * 64
ARCHIVE = 'b' * 64
EXPORT = 'full-export-' + 'c' * 32


class Clock:
    def __init__(self):
        self.now = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def __call__(self):
        return self.now

    def advance(self, **kwargs):
        self.now += timedelta(**kwargs)


@pytest.fixture
def setup(tmp_path):
    clock = Clock()
    store = WorkspaceStore(tmp_path / 'synthetic.sqlite', clock=clock)
    for name, ident in [('owner', OWNER), ('member', MEMBER), ('admin', ADMIN)]:
        store.upsert_principal('test', name, 'Generated ' + name, name, preferred_principal_id=ident)
    matter = store.create_matter(NAME, 'Generated fixture', OWNER)
    store.add_member(matter.matter_id, MEMBER, OWNER)
    yield store, matter, clock
    store.close()


def session(store, actor=OWNER, *, administrator=False):
    end = store._timestamp(store.current_time() + timedelta(days=30))
    return store.create_session(actor, hashlib.sha256(uuid.uuid4().bytes).hexdigest(), 'test', end, end,
                                frozenset({'administrator'}) if administrator else frozenset())


def exported_exit(store, matter):
    case = store.case_lifecycle.begin_exit(matter.slug, OWNER, session_id=lifecycle_session(store, OWNER, administrator_override=False).session_id)
    current = store.case_lifecycle.register_export(matter.matter_id, OWNER, case.exit_id,
                                               EXPORT, ARCHIVE, DIGEST, session_id=lifecycle_session(store, OWNER, administrator_override=False).session_id)

    from tests.case_lifecycle_helpers import record_synthetic_delivery
    record_synthetic_delivery(store, current, OWNER, session(store).session_id)
    return current


def claim(store, matter, sess, nonce, **overrides):
    args = dict(token=nonce.token, session_id=sess.session_id, export_id=EXPORT,
                export_acknowledged=True, source_count=2, current_snapshot_digest=DIGEST)
    name = overrides.pop('confirmed_name', NAME)
    args.update(overrides)
    return store.case_lifecycle.claim_purge(matter.slug, OWNER, name, **args)


def test_new_ongoing_checkins_never_create_delete_schedule(setup):
    store, matter, clock = setup
    policy = store.case_lifecycle.policy(matter.matter_id)
    assert policy.mode == 'ongoing'
    assert policy.next_check_in_at == '2026-04-01T00:00:00Z'
    assert store.matter_retention(matter.matter_id) is None
    clock.advance(days=400)
    assert store.due_matter_retentions() == ()
    assert store.begin_due_matter_purge(matter.matter_id, source_count=0) is None
    assert store.matter_lifecycle(matter.matter_id).state == 'active'
    policy = store.case_lifecycle.check_in(matter.matter_id, OWNER, session_id=lifecycle_session(store, OWNER, administrator_override=False).session_id)
    assert policy.next_check_in_at == store._timestamp(clock.now + timedelta(days=90))
    with pytest.raises(WorkspaceProblem, match='check-in'):
        store.set_matter_retention(matter.matter_id, OWNER, clock.now + timedelta(days=30))
    with pytest.raises(WorkspaceProblem, match='full export'):
        store.begin_matter_purge(matter.slug, OWNER, NAME, source_count=0)


def test_seven_days_and_fresh_confirmation_required_without_timer_purge(setup):
    store, matter, clock = setup
    case = exported_exit(store, matter)
    assert case.eligible_after == '2026-01-08T00:00:00Z'
    sess = session(store)
    for delta in [timedelta(), timedelta(days=7, microseconds=-1)]:
        clock.now = datetime(2026, 1, 1, tzinfo=timezone.utc) + delta
        with pytest.raises(WorkspaceProblem, match='seven-day'):
            store.case_lifecycle.issue_confirmation(matter.slug, OWNER, sess.session_id, EXPORT)
    clock.advance(microseconds=1)
    assert store.begin_due_matter_purge(matter.matter_id, source_count=0) is None
    assert store.matter_lifecycle(matter.matter_id).state == 'closing'
    nonce = store.case_lifecycle.issue_confirmation(matter.slug, OWNER, sess.session_id, EXPORT)
    _, lifecycle = claim(store, matter, sess, nonce)
    assert lifecycle.state == 'purging'
    assert store.case_lifecycle.validate_purge(matter.matter_id, lifecycle.purge_id).exit_id == case.exit_id
    with pytest.raises(WorkspaceProblem):
        claim(store, matter, sess, nonce)


def test_export_failure_closing_and_active_work_fail_closed(setup):
    store, matter, clock = setup
    conversation = store.get_conversation(matter.matter_id)
    job, _ = store.queue_answer_job(matter.matter_id, conversation.conversation_id, OWNER,
                                    'Generated question', 'answer-request-' + 'd' * 32)
    with pytest.raises(WorkspaceProblem, match='Wait for current'):
        store.case_lifecycle.begin_exit(matter.slug, OWNER, session_id=lifecycle_session(store, OWNER, administrator_override=False).session_id)
    assert store.matter_lifecycle(matter.matter_id).state == 'active'
    store.cancel_answer_job(matter.matter_id, OWNER, job.job_id)
    store.case_lifecycle.begin_exit(matter.slug, OWNER, session_id=lifecycle_session(store, OWNER, administrator_override=False).session_id)
    with pytest.raises(KeyError):
        store.membership(matter.matter_id, OWNER)
    with pytest.raises((KeyError, WorkspaceProblem)):
        store.queue_answer_job(matter.matter_id, conversation.conversation_id, OWNER,
                               'Generated new question', 'answer-request-' + 'e' * 32)
    assert store.case_lifecycle.read_matter(matter.slug, MEMBER).matter_id == matter.matter_id
    clock.advance(days=7)
    sess = session(store)
    with pytest.raises(WorkspaceProblem, match='verified full export'):
        store.case_lifecycle.issue_confirmation(matter.slug, OWNER, sess.session_id, EXPORT)


def test_cancel_invalidates_export_nonce_and_stale_generation(setup):
    store, matter, clock = setup
    case = exported_exit(store, matter)
    assert store.case_lifecycle.begin_exit(matter.slug, OWNER, session_id=lifecycle_session(store, OWNER, administrator_override=False).session_id).exit_id == case.exit_id
    clock.advance(days=7)
    sess = session(store)
    nonce = store.case_lifecycle.issue_confirmation(matter.slug, OWNER, sess.session_id, EXPORT)
    store.case_lifecycle.cancel_exit(matter.slug, OWNER, case.exit_id, session_id=lifecycle_session(store, OWNER, administrator_override=False).session_id)
    assert store.matter_lifecycle(matter.matter_id).state == 'active'
    assert store.case_lifecycle.current_exit(matter.matter_id) is None
    new = store.case_lifecycle.begin_exit(matter.slug, OWNER, session_id=lifecycle_session(store, OWNER, administrator_override=False).session_id)
    assert new.exit_id != case.exit_id
    assert new.eligible_after == '2026-01-15T00:00:00Z'
    with pytest.raises(WorkspaceProblem, match='exit changed'):
        store.case_lifecycle.register_export(matter.matter_id, OWNER, case.exit_id,
                                             EXPORT, ARCHIVE, DIGEST, session_id=lifecycle_session(store, OWNER, administrator_override=False).session_id)
    with pytest.raises(WorkspaceProblem):
        claim(store, matter, sess, nonce)
    assert store.connection.execute('SELECT COUNT(*) FROM workbench_case_confirmation').fetchone()[0] == 0


@pytest.mark.parametrize('invalid', ['expired', 'session', 'principal', 'nonce', 'snapshot', 'acknowledgement', 'name'])
def test_fresh_claim_rechecks_each_authorization_boundary(setup, invalid):
    store, matter, clock = setup
    exported_exit(store, matter)
    clock.advance(days=7)
    sess = session(store)
    nonce = store.case_lifecycle.issue_confirmation(matter.slug, OWNER, sess.session_id, EXPORT)
    overrides = {}
    if invalid == 'expired':
        clock.advance(minutes=10)
    elif invalid == 'session':
        store.revoke_session(sess.session_id)
    elif invalid == 'principal':
        with fixture_write(store):
            store.connection.execute('UPDATE workbench_principal SET active=0 WHERE principal_id=?', (OWNER,))
    elif invalid == 'nonce':
        overrides['token'] = 'invalid-' + 'x' * 32
    elif invalid == 'snapshot':
        overrides['current_snapshot_digest'] = 'd' * 64
    elif invalid == 'acknowledgement':
        overrides['export_acknowledged'] = False
    else:
        overrides['confirmed_name'] = 'Synthetic wrong name'
    with pytest.raises((WorkspaceProblem, KeyError)):
        claim(store, matter, sess, nonce, **overrides)
    assert store.matter_lifecycle(matter.matter_id).state == 'closing'
    assert not store.connection.in_transaction


def test_member_cannot_exit_and_admin_requires_current_session_role(setup):
    store, matter, clock = setup
    with pytest.raises(KeyError):
        store.case_lifecycle.begin_exit(matter.slug, MEMBER, session_id=lifecycle_session(store, MEMBER, administrator_override=False).session_id)
    exported_exit(store, matter)
    clock.advance(days=7)
    sess = session(store, ADMIN)
    with pytest.raises(WorkspaceProblem, match='administrator authority'):
        store.case_lifecycle.issue_confirmation(matter.slug, ADMIN, sess.session_id, EXPORT,
                                               administrator_override=True)
    with fixture_write(store):
        store.connection.execute("UPDATE workbench_session SET application_roles='[\"administrator\"]' WHERE session_id=?",
                                 (sess.session_id,))
    from tests.case_lifecycle_helpers import record_synthetic_delivery
    record_synthetic_delivery(store, store.case_lifecycle.current_exit(matter.matter_id), ADMIN,
                              sess.session_id, administrator_override=True)
    nonce = store.case_lifecycle.issue_confirmation(matter.slug, ADMIN, sess.session_id, EXPORT,
                                                   administrator_override=True)
    _, lifecycle = store.case_lifecycle.claim_purge(matter.slug, ADMIN, NAME, token=nonce.token,
        session_id=sess.session_id, export_id=EXPORT, export_acknowledged=True, source_count=0,
        current_snapshot_digest=DIGEST, administrator_override=True)
    assert lifecycle.requested_by == ADMIN


def test_confirmed_interrupted_purge_retries_but_legacy_failed_never_does(setup):
    store, matter, clock = setup
    exported_exit(store, matter)
    clock.advance(days=7)
    sess = session(store)
    nonce = store.case_lifecycle.issue_confirmation(matter.slug, OWNER, sess.session_id, EXPORT)
    _, started = claim(store, matter, sess, nonce)
    assert store.recover_interrupted_matter_purges() == 1
    with pytest.raises(WorkspaceProblem):
        store.case_lifecycle.cancel_exit(matter.slug, OWNER, store.case_lifecycle.current_exit(matter.matter_id).exit_id, session_id=lifecycle_session(store, OWNER, administrator_override=False).session_id)
    retry = store.case_lifecycle.issue_confirmation(matter.slug, OWNER, sess.session_id, EXPORT)
    _, resumed = claim(store, matter, sess, retry)
    assert resumed.purge_id == started.purge_id
    completed = store.complete_matter_purge(matter.matter_id, started.purge_id)
    assert completed.state == 'deleted'
    assert store.case_lifecycle.current_exit(matter.matter_id) is None
    legacy = store.create_matter('Synthetic legacy interrupted', '', OWNER)
    with fixture_write(store):
        store.connection.execute("UPDATE workbench_matter_lifecycle SET state='purge_failed',purge_id=? WHERE matter_id=?",
                                 ('matter-purge-' + 'f' * 32, legacy.matter_id))
    with pytest.raises(WorkspaceProblem, match='Start an explicit exit'):
        store.case_lifecycle.issue_confirmation(legacy.slug, OWNER, sess.session_id, EXPORT)
    with pytest.raises(WorkspaceProblem, match='confirmed current exit'):
        store.case_lifecycle.validate_purge(legacy.matter_id, 'matter-purge-' + 'f' * 32)


def test_concurrent_claims_have_one_winner_and_token_is_private(setup):
    store, matter, clock = setup
    exported_exit(store, matter)
    clock.advance(days=7)
    sess = session(store)
    nonce = store.case_lifecycle.issue_confirmation(matter.slug, OWNER, sess.session_id, EXPORT)
    other = WorkspaceStore(store.path, clock=clock)
    barrier = threading.Barrier(2)
    outcomes = []

    def run(workspace):
        barrier.wait(timeout=5)
        try:
            claim(workspace, matter, sess, nonce)
            outcomes.append('claimed')
        except WorkspaceProblem:
            outcomes.append('refused')

    threads = [threading.Thread(target=run, args=(workspace,)) for workspace in (store, other)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
        assert not thread.is_alive()
    assert sorted(outcomes) == ['claimed', 'refused']
    audit = repr(store.audit_events(matter.matter_id))
    assert nonce.token not in audit and NAME not in audit
    assert DIGEST not in audit and ARCHIVE not in audit
    other.close()


def test_old_connection_cannot_write_lifecycle(setup):
    store, matter, _ = setup
    old = sqlite3.connect(store.path)
    with pytest.raises(sqlite3.OperationalError, match=f"{LEGACY_STEM}_case_lifecycle_writer"):
        # fixture-write: An independent old writer must exercise the lifecycle SQL fence.
        old.execute("UPDATE workbench_matter_lifecycle SET state='purging' WHERE matter_id=?", (matter.matter_id,))
    old.close()
    assert store.matter_lifecycle(matter.matter_id).state == 'active'


def test_window_compares_instants_with_fractional_seconds(setup):
    store, matter, clock = setup
    clock.advance(microseconds=100000)
    exported_exit(store, matter)
    clock.advance(days=7, microseconds=-100000)
    sess = session(store)
    # Lexical ISO comparisons would incorrectly permit the same whole second.
    with pytest.raises(WorkspaceProblem, match='seven-day'):
        store.case_lifecycle.issue_confirmation(matter.slug, OWNER, sess.session_id, EXPORT)
    clock.advance(microseconds=100000)
    nonce = store.case_lifecycle.issue_confirmation(matter.slug, OWNER, sess.session_id, EXPORT)
    assert claim(store, matter, sess, nonce)[1].state == 'purging'


def test_nonce_binds_displayed_name_and_repeated_cancel_is_harmless(setup):
    store, matter, clock = setup
    current = exported_exit(store, matter)
    clock.advance(days=7)
    sess = session(store)
    nonce = store.case_lifecycle.issue_confirmation(matter.slug, OWNER, sess.session_id, EXPORT)
    with pytest.raises(sqlite3.IntegrityError, match='frozen'):
        with fixture_write(store):
            store.connection.execute('UPDATE workbench_matter SET display_name=? WHERE matter_id=?',
                                     ('Synthetic renamed matter', matter.matter_id))
    with store._lock:
        saved_name = store.connection.execute('SELECT name_digest FROM workbench_case_confirmation').fetchone()[0]
    assert saved_name == hashlib.sha256(NAME.encode()).hexdigest()
    with pytest.raises(WorkspaceProblem, match='exactly'):
        store.case_lifecycle.claim_purge(matter.slug, OWNER, 'Synthetic renamed matter',
            token=nonce.token, session_id=sess.session_id, export_id=EXPORT, export_acknowledged=True,
            source_count=0, current_snapshot_digest=DIGEST)
    first = store.case_lifecycle.cancel_exit(matter.slug, OWNER, current.exit_id, session_id=lifecycle_session(store, OWNER, administrator_override=False).session_id)
    assert store.case_lifecycle.cancel_exit(matter.slug, OWNER, current.exit_id, session_id=lifecycle_session(store, OWNER, administrator_override=False).session_id) == first
    assert store.matter_lifecycle(matter.matter_id).state == 'active'


def test_audit_failure_rolls_back_exit_and_confirmation_claim(setup, monkeypatch):
    store, matter, clock = setup
    append = store._append_audit_event_locked

    def fail_audit(**kwargs):
        raise sqlite3.OperationalError('Synthetic audit failure')

    monkeypatch.setattr(store, '_append_audit_event_locked', fail_audit)
    with pytest.raises(sqlite3.OperationalError, match='Synthetic audit failure'):
        store.case_lifecycle.begin_exit(matter.slug, OWNER, session_id=lifecycle_session(store, OWNER, administrator_override=False).session_id)
    assert store.matter_lifecycle(matter.matter_id).state == 'active'
    assert store.case_lifecycle.current_exit(matter.matter_id) is None
    monkeypatch.setattr(store, '_append_audit_event_locked', append)
    exported_exit(store, matter)
    clock.advance(days=7)
    sess = session(store)
    nonce = store.case_lifecycle.issue_confirmation(matter.slug, OWNER, sess.session_id, EXPORT)
    stored = store.connection.execute('SELECT token_digest FROM workbench_case_confirmation').fetchone()[0]
    assert stored == hashlib.sha256(nonce.token.encode()).hexdigest()
    assert stored != nonce.token
    monkeypatch.setattr(store, '_append_audit_event_locked', fail_audit)
    with pytest.raises(sqlite3.OperationalError, match='Synthetic audit failure'):
        claim(store, matter, sess, nonce)
    assert store.matter_lifecycle(matter.matter_id).state == 'closing'
    assert store.case_lifecycle.current_exit(matter.matter_id).state == 'closing'
    assert store.connection.execute('SELECT COUNT(*) FROM workbench_case_confirmation').fetchone()[0] == 1
    monkeypatch.setattr(store, '_append_audit_event_locked', append)
    assert claim(store, matter, sess, nonce)[1].state == 'purging'


def test_cancellation_and_confirmation_have_one_serialized_winner(setup):
    store, matter, clock = setup
    current = exported_exit(store, matter)
    clock.advance(days=7)
    sess = session(store)
    nonce = store.case_lifecycle.issue_confirmation(matter.slug, OWNER, sess.session_id, EXPORT)
    other = WorkspaceStore(store.path, clock=clock)
    barrier = threading.Barrier(2)
    outcomes = []

    def cancel():
        barrier.wait(timeout=5)
        try:
            other.case_lifecycle.cancel_exit(matter.slug, OWNER, current.exit_id, session_id=lifecycle_session(other, OWNER, administrator_override=False).session_id)
            outcomes.append('cancelled')
        except WorkspaceProblem:
            outcomes.append('cancel_refused')

    def purge():
        barrier.wait(timeout=5)
        try:
            claim(store, matter, sess, nonce)
            outcomes.append('claimed')
        except WorkspaceProblem:
            outcomes.append('claim_refused')

    threads = [threading.Thread(target=cancel), threading.Thread(target=purge)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
        assert not thread.is_alive()
    assert set(outcomes) in ({'cancelled', 'claim_refused'}, {'claimed', 'cancel_refused'})
    state = store.matter_lifecycle(matter.matter_id).state
    assert state == ('active' if 'cancelled' in outcomes else 'purging')
    assert store.connection.execute('SELECT COUNT(*) FROM workbench_case_confirmation').fetchone()[0] == 0
    other.close()


@pytest.mark.parametrize('invalid', ['different_session', 'different_export', 'revoked_owner', 'provider_disabled'])
def test_confirmation_uses_live_owner_session_export_and_provider(setup, invalid):
    store, matter, clock = setup
    exported_exit(store, matter)
    clock.advance(days=7)
    sess = session(store)
    nonce = store.case_lifecycle.issue_confirmation(matter.slug, OWNER, sess.session_id, EXPORT)
    options = {}
    if invalid == 'different_session':
        clock.advance(seconds=1)
        options['session_id'] = session(store).session_id
    elif invalid == 'different_export':
        options['export_id'] = 'full-export-' + 'd' * 32
    elif invalid == 'revoked_owner':
        with fixture_write(store):
            store.connection.execute("UPDATE workbench_matter_membership SET state='revoked' "
                                     'WHERE matter_id=? AND principal_id=?', (matter.matter_id, OWNER))
    else:
        store.principal_enabled = lambda provider, subject: False
    with pytest.raises((KeyError, WorkspaceProblem)):
        claim(store, matter, sess, nonce, **options)
    assert store.matter_lifecycle(matter.matter_id).state == 'closing'
