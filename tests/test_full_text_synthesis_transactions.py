"""Force synthesis fixture access to overlap a real report claim transaction."""
import threading

import pytest

from tests import test_full_text_synthesis as synthesis
from tests.test_full_text_synthesis import workspace  # noqa: F401


@pytest.mark.parametrize('boundary', ['fixture_write', 'rollback_assertion'])
def test_synthesis_fixture_serializes_with_report_claim(workspace, monkeypatch, boundary):
    _, bench, _, _ = workspace
    store = bench.workspace
    lock = store._lock
    test_thread = threading.current_thread()
    attempted = threading.Event()
    in_transaction = threading.Event()
    release = threading.Event()
    errors = []
    started = False

    class ObservedLock:
        def __enter__(self):
            current = threading.current_thread()
            if current is claimant:
                attempted.set()
            elif current is test_thread and started:
                # A correct fixture waits on this lock. Let the claim finish
                # when that wait begins, without a sleep or scheduling window.
                release.set()
            return lock.__enter__()

        def __exit__(self, *args):
            return lock.__exit__(*args)

    recover = bench.report_compilation.jobs._recover

    def paused_recover(connection, now):
        if threading.current_thread() is claimant:
            # claim() has acquired the workspace lock and executed its real
            # BEGIN IMMEDIATE, even though this synthetic report queue is empty.
            assert connection.in_transaction
            in_transaction.set()
            assert release.wait(5), 'synthesis fixture did not reach the claim boundary'
            assert connection.in_transaction, 'synthesis fixture committed the report claim transaction'
        return recover(connection, now)

    def claim():
        try:
            assert bench.report_compilation.jobs.claim('synthetic-overlap-worker') is None
        except BaseException as exc:
            errors.append(exc)

    claimant = threading.Thread(target=claim, daemon=True)
    monkeypatch.setattr(store, '_lock', ObservedLock())
    monkeypatch.setattr(bench.report_compilation.jobs, '_recover', paused_recover)

    def start_overlap():
        nonlocal started
        guarded = lock._is_owned()
        started = True
        claimant.start()
        if guarded:
            # The rollback assertion owns the lock across the rejected save.
            assert attempted.wait(5), 'report claim did not attempt the workspace lock'
        else:
            assert in_transaction.wait(5), 'report claim did not begin its transaction'

    if boundary == 'fixture_write':
        seed = synthesis.seed_terminal_review

        def seeded(*args, **kwargs):
            result = seed(*args, **kwargs)
            start_overlap()
            return result

        monkeypatch.setattr(synthesis, 'seed_terminal_review', seeded)
        exercise = lambda: synthesis.test_active_review_is_rejected_before_any_synthesis_job(workspace, 'queued')
    else:
        finish = bench._finish_research_job

        def finished(*args, **kwargs):
            try:
                return finish(*args, **kwargs)
            finally:
                start_overlap()

        monkeypatch.setattr(bench, '_finish_research_job', finished)
        exercise = lambda: synthesis.test_final_transaction_and_export_revalidate_sources_and_access(workspace, 'source')

    try:
        exercise()
    finally:
        release.set()
        if started:
            claimant.join(5)
        assert not claimant.is_alive(), 'report claim did not finish'
    assert in_transaction.is_set(), 'report claim transaction was not exercised'
    assert not errors, errors
