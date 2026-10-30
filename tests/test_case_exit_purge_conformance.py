"""Synthetic replays of CaseExitPurge actions against the real repositories."""

import sqlite3
from datetime import timedelta

import pytest

from case_intelligence.case_lifecycle_authority import require_current_session_authority
from case_intelligence.lifecycle import ArchiveVerificationError, ClosureStage, TrustedArchiveAdapter
from case_intelligence.workspace_store import WorkspaceProblem
from tests.case_lifecycle_helpers import record_synthetic_delivery
from tests.fixture_write import fixture_write
from tests.support import ALPHA, NOW
from tests.test_case_lifecycle import ARCHIVE, DIGEST, EXPORT, NAME, OWNER, session, setup  # noqa: F401
from tests.test_lifecycle import closure_store, request

MODES = ['export_exit', 'delete_without_export']


class Trace:
    def __init__(self, setup, mode):
        self.store, self.matter, self.clock = setup
        self.repo = self.store.case_lifecycle
        self.mode = mode
        self.session = session(self.store).session_id
        self.exit = self.repo.begin_exit(self.matter.slug, OWNER, mode=mode, session_id=self.session)

    def register(self):
        self.exit = self.repo.register_export(self.matter.matter_id, OWNER, self.exit.exit_id,
            EXPORT, ARCHIVE, DIGEST, session_id=self.session)

    def deliver(self):
        record_synthetic_delivery(self.store, self.exit, OWNER, self.session)

    def prepare(self):
        if self.mode == 'export_exit':
            self.register()
            self.deliver()

    def issue(self, **changes):
        args = dict(mode=self.mode, export_id=EXPORT if self.mode == 'export_exit' else None)
        args.update(changes)
        return self.repo.issue_confirmation(self.matter.slug, OWNER, self.session, **args)

    def claim(self, token, **changes):
        args = dict(token=token, session_id=self.session, mode=self.mode,
            export_id=EXPORT if self.mode == 'export_exit' else None,
            export_acknowledged=True, no_backup_acknowledged=True,
            current_snapshot_digest=DIGEST, source_count=0)
        name = changes.pop('confirmed_name', NAME)
        args.update(changes)
        return self.repo.claim_purge(self.matter.slug, OWNER, name, **args)[1]

    def cancel(self):
        self.repo.cancel_exit(self.matter.slug, OWNER, self.exit.exit_id, session_id=self.session)


def test_s1_export_registration_digest_and_receipt(setup):
    t = Trace(setup, 'export_exit')
    t.clock.advance(days=7)
    with pytest.raises(WorkspaceProblem, match='verified full export'):
        t.issue()
    with pytest.raises(ValueError, match='Invalid verified export'):
        t.repo.register_export(t.matter.matter_id, OWNER, t.exit.exit_id,
            EXPORT, '', DIGEST, session_id=t.session)
    t.register()
    attempt = t.repo.downloads.begin(t.matter.matter_id, OWNER, t.session,
        EXPORT, ARCHIVE, 100, 0, 50)
    assert not t.repo.downloads.complete(attempt)
    with pytest.raises(WorkspaceProblem, match='Download the complete'):
        t.issue()
    t.deliver()
    claimed = t.claim(t.issue().token)
    assert t.repo.validate_purge(t.matter.matter_id, claimed.purge_id).archive_digest == ARCHIVE


@pytest.mark.parametrize('invalid', [{'no_backup_acknowledged': False},
    {'no_backup_acknowledged': 1}])
def test_s1b_explicit_no_export_admission(setup, invalid):
    t = Trace(setup, 'delete_without_export')
    t.clock.advance(days=7)
    token = t.issue().token
    with pytest.raises(WorkspaceProblem):
        t.claim(token, **invalid)
    claimed = t.claim(token)
    authority = t.repo.validate_purge(t.matter.matter_id, claimed.purge_id)
    assert authority.mode == t.mode and authority.export_id is None


@pytest.mark.parametrize('confirmed_name', [
    'Synthetic other matter', NAME.swapcase(), NAME + ' ', ' ' + NAME])
def test_s1b_exact_name(setup, confirmed_name):
    t = Trace(setup, 'delete_without_export')
    t.clock.advance(days=7)
    token = t.issue().token
    with pytest.raises(WorkspaceProblem, match='name exactly as shown'):
        t.claim(token, confirmed_name=confirmed_name)
    assert t.claim(token).state == 'purging'


@pytest.mark.parametrize('mode', MODES)
def test_s3_confirmation_bound_to_mode(setup, mode):
    t = Trace(setup, mode)
    t.prepare()
    t.clock.advance(days=7)
    token = t.issue().token
    other_mode = next(value for value in MODES if value != mode)
    with fixture_write(t.store):
        # fixture-write: Corrupt only the synthetic nonce mode, keeping exit readiness valid.
        t.store.connection.execute(
            'UPDATE workbench_case_confirmation SET mode=? WHERE exit_id=?',
            (other_mode, t.exit.exit_id))
    with pytest.raises(WorkspaceProblem, match='Request a fresh deletion confirmation'):
        t.claim(token)
    assert t.claim(t.issue().token).state == 'purging'


@pytest.mark.parametrize('registered', [False, True])
def test_s1c_mode_change_requires_cancel_and_new_window(setup, registered):
    t = Trace(setup, 'export_exit')
    if registered:
        t.register()
    t.clock.advance(days=7)
    mode = 'delete_without_export'
    with pytest.raises(WorkspaceProblem, match='Cancel closure'):
        t.repo.begin_exit(t.matter.slug, OWNER, mode=mode, session_id=t.session)
    with pytest.raises(WorkspaceProblem, match='chosen closure mode'):
        t.issue(mode=mode, export_id=None)
    with pytest.raises(WorkspaceProblem, match='chosen closure mode'):
        t.claim('x' * 32, mode=mode, export_id=None)
    assert t.repo.current_exit(t.matter.matter_id).mode == 'export_exit'
    t.cancel()
    new = t.repo.begin_exit(t.matter.slug, OWNER, mode=mode, session_id=t.session)
    assert new.exit_id != t.exit.exit_id
    assert new.eligible_after == t.store._timestamp(t.clock.now + timedelta(days=7))
    with pytest.raises(WorkspaceProblem, match='seven-day'):
        t.issue(mode=mode, export_id=None)


@pytest.mark.parametrize('mode', MODES)
def test_s2_exact_cancellation_boundary(setup, mode):
    t = Trace(setup, mode)
    t.prepare()
    t.clock.advance(days=7, microseconds=-1)
    with pytest.raises(WorkspaceProblem, match='seven-day'):
        t.issue()
    with pytest.raises(WorkspaceProblem, match='seven-day'):
        t.claim('x' * 32)
    t.clock.advance(microseconds=1)
    assert t.claim(t.issue().token).state == 'purging'


@pytest.mark.parametrize('mode', MODES)
def test_s3_confirmation_just_before_expiry(setup, mode):
    t = Trace(setup, mode)
    t.prepare()
    t.clock.advance(days=7)
    token = t.issue().token
    t.clock.advance(minutes=10, microseconds=-1)
    assert t.claim(token).state == 'purging'


@pytest.mark.parametrize('mode', MODES)
@pytest.mark.parametrize('invalidate', ['expiry', 'restart', 'session'])
def test_s3_confirmation_expiry_restart_and_live_authority(setup, mode, invalidate):
    t = Trace(setup, mode)
    t.prepare()
    t.clock.advance(days=7)
    token = t.issue().token
    if invalidate == 'expiry':
        t.clock.advance(minutes=10)
    elif invalidate == 'restart':
        t.repo.discard_confirmations_on_startup()
    else:
        require_current_session_authority(t.store, OWNER, t.session, False)
        t.store.revoke_session(t.session)
        with pytest.raises(WorkspaceProblem):
            require_current_session_authority(t.store, OWNER, t.session, False)
    with pytest.raises(WorkspaceProblem):
        t.claim(token)
    t.session = session(t.store).session_id
    claimed = t.claim(t.issue().token)
    with pytest.raises(WorkspaceProblem):
        t.claim(token)
    assert t.repo.validate_purge(t.matter.matter_id, claimed.purge_id).exit_id == t.exit.exit_id


@pytest.mark.parametrize('mode', MODES)
def test_s4_cancel_revokes_exit_and_stale_forms_and_reopens_fence(setup, mode):
    t = Trace(setup, mode)
    t.prepare()
    t.clock.advance(days=7)
    token = t.issue().token
    with pytest.raises(sqlite3.IntegrityError, match='frozen'):
        with fixture_write(t.store):
            # fixture-write: Exercise the real installed content fence on synthetic state.
            t.store.connection.execute('UPDATE workbench_matter SET display_name=? WHERE matter_id=?',
                (NAME, t.matter.matter_id))
    t.cancel()
    with pytest.raises(WorkspaceProblem):
        t.claim(token)
    with fixture_write(t.store):
        # fixture-write: The same synthetic write succeeds after cancellation.
        t.store.connection.execute('UPDATE workbench_matter SET display_name=? WHERE matter_id=?',
            (NAME, t.matter.matter_id))
    replacement = Trace(setup, mode)
    assert replacement.exit.exit_id != t.exit.exit_id
    replacement.prepare()
    t.clock.advance(days=7)
    with pytest.raises(WorkspaceProblem):
        replacement.claim(token)
    claimed = replacement.claim(replacement.issue().token)
    assert replacement.repo.validate_purge(t.matter.matter_id, claimed.purge_id).exit_id != t.exit.exit_id


@pytest.mark.parametrize('mode', MODES)
@pytest.mark.parametrize('failure', ['reported', 'interrupted'])
def test_s5_l1_retry_keeps_identity_and_resolves(setup, mode, failure):
    t = Trace(setup, mode)
    t.prepare()
    t.clock.advance(days=7)
    claimed = t.claim(t.issue().token)
    if failure == 'reported':
        assert t.store.fail_matter_purge(t.matter.matter_id, claimed.purge_id, 'storage').state == 'purge_failed'
    else:
        t.repo.discard_confirmations_on_startup()
        assert t.store.recover_interrupted_matter_purges() == 1
    with pytest.raises(WorkspaceProblem):
        t.cancel()
    with pytest.raises(WorkspaceProblem):
        t.claim('x' * 32)
    retried = t.claim(t.issue().token)
    assert retried.purge_id == claimed.purge_id
    assert t.store.complete_matter_purge(t.matter.matter_id, retried.purge_id).state == 'deleted'


def test_s6_closure_order_and_trusted_verification():
    store, _ = closure_store()
    run = request(store)
    stages = list(ClosureStage)[:-1]
    adapter = TrustedArchiveAdapter(clock=lambda: NOW, attestation_key=b'synthetic-only')
    receipt = adapter.verify(ALPHA, run.closure_run_id,
        run.archive_destination.archive_destination_id, run.policy_revision)
    for index, stage in enumerate(stages[1:], 1):
        with pytest.raises(ValueError):
            store.advance(run.closure_run_id, stages[(index + 1) % len(stages)])
        if stage == ClosureStage.ARCHIVE_VERIFIED:
            with pytest.raises(ValueError):
                store.advance(run.closure_run_id, stage)
            with pytest.raises(ArchiveVerificationError):
                store.verify_archive(run.closure_run_id, receipt)
            store.trust_adapter(adapter)
            assert store.verify_archive(run.closure_run_id, receipt).stage == stage
        else:
            assert store.advance(run.closure_run_id, stage).stage == stage
    assert store.closure_record(run.closure_run_id).receipt_id == receipt.receipt_id
    with pytest.raises(ValueError):
        store.cancel(run.closure_run_id)
    other, _ = closure_store()
    cancelled = other.cancel(request(other).closure_run_id)
    assert cancelled.stage == ClosureStage.CANCELLED
    with pytest.raises(ValueError):
        other.advance(cancelled.closure_run_id, ClosureStage.EXPORTING)
