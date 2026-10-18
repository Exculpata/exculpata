"""Synthetic durable queue, fencing, cancellation, and restore regressions."""

from tests.fixture_write import fixture_write
import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from case_intelligence.report_compilation import CompilationProblem
from case_intelligence.report_compilation_jobs import (
    CompilationJobPolicy, CompilationLeaseLost, ReportCompilationCoordinator, ReportCompilationJobs,
)
from case_intelligence.workspace_store import WorkspaceStore

ACTOR = "synthetic-compilation-reviewer"
FINGERPRINT = "a" * 64
ROOT = Path(__file__).parents[1]
MIGRATION = ROOT / "src/case_intelligence/migrations/sqlite/0027_report_compilation.sql"


def setup_store(path):
    store = WorkspaceStore(path)
    with store._lock:
        store.connection.executescript(MIGRATION.read_text())  # fixture-write: Replay the existing migration on an isolated store before workers start; its foreign-key settings require raw migration semantics.
    actor = store.upsert_principal("test", ACTOR, "Synthetic reviewer", ACTOR, preferred_principal_id=ACTOR)
    matter = store.create_matter("Generated compilation matter", "Synthetic", actor.principal_id)
    return store, matter


def queue(jobs, matter, key="a"):
    return jobs.queue(matter.matter_id, ACTOR, "timeline", "", ("research:synthetic-selection",), "compile-request-" + key * 32)[0]


def build_report(store, matter, identifier="report-synthetic-completed"):
    now = store._now()
    with fixture_write(store):
        store.connection.execute(
            "INSERT INTO workbench_report(report_id,matter_id,title,purpose,status,created_by,created_at,updated_by,updated_at) VALUES (?,?,?,?,'draft',?,?,?,?)",
            (identifier, matter.matter_id, "Generated compiled report", "Synthetic", ACTOR, now, ACTOR, now))
    return identifier


def test_migration_is_mirrored_idempotent_and_keeps_old_records(tmp_path):
    assert MIGRATION.read_bytes() == (ROOT / "migrations/sqlite/0027_report_compilation.sql").read_bytes()
    store, matter = setup_store(tmp_path / "control.sqlite")
    jobs = ReportCompilationJobs(store)
    first = queue(jobs, matter)
    with store._lock:
        store.connection.executescript(MIGRATION.read_text())  # fixture-write: Check migration idempotency on an isolated store with no worker; preserve the migration's foreign-key configuration semantics.
    assert jobs.get(matter.matter_id, ACTOR, first.job_id).state == "queued"
    assert store.connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    store.close()


def test_request_idempotency_binds_kind_topic_and_selection(tmp_path):
    store, matter = setup_store(tmp_path / "control.sqlite")
    jobs = ReportCompilationJobs(store)
    first = queue(jobs, matter)
    repeated, created = jobs.queue(matter.matter_id, ACTOR, "timeline", "", ("research:synthetic-selection",), first.request_key)
    assert not created and repeated.job_id == first.job_id
    with pytest.raises(CompilationProblem, match="different compilation inputs"):
        jobs.queue(matter.matter_id, ACTOR, "entities", "", first.selections, first.request_key)
    store.close()


def test_two_processes_can_claim_each_job_only_once(tmp_path):
    path = tmp_path / "control.sqlite"
    store, matter = setup_store(path)
    identifier = queue(ReportCompilationJobs(store), matter).job_id
    store.close()
    program = """
import json,sys,uuid
from case_intelligence.workspace_store import WorkspaceStore
from case_intelligence.report_compilation_jobs import ReportCompilationJobs
store=WorkspaceStore(sys.argv[1])
job=ReportCompilationJobs(store).claim('synthetic-worker-'+uuid.uuid4().hex)
print(json.dumps(job.job_id if job else None))
store.close()
"""
    processes = [subprocess.Popen([sys.executable, "-c", program, str(path)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env={**os.environ, "PYTHONPATH": str(ROOT / "src")}) for _ in range(2)]
    results = []
    for process in processes:
        stdout, stderr = process.communicate(timeout=20)
        assert process.returncode == 0, stderr
        results.append(json.loads(stdout))
    assert results.count(identifier) == 1 and results.count(None) == 1


def test_expired_lease_is_reclaimed_and_stale_worker_cannot_save(tmp_path):
    store, matter = setup_store(tmp_path / "control.sqlite")
    clock = [100.0]
    jobs = ReportCompilationJobs(store, policy=CompilationJobPolicy(lease_seconds=10), clock=lambda: clock[0])
    queue(jobs, matter)
    first = jobs.claim("synthetic-first")
    jobs.record_input_fingerprint(first, FINGERPRINT)
    assert jobs.claim("synthetic-second") is None
    clock[0] = 111.0
    second = jobs.claim("synthetic-second")
    assert second.job_id == first.job_id and second.lease_token != first.lease_token
    assert second.attempts == 2 and second.input_fingerprint == ""
    assert jobs.heartbeat(first) is False
    with pytest.raises(CompilationLeaseLost):
        jobs.complete(first, lambda: build_report(store, matter), fingerprint=FINGERPRINT)
    assert store.connection.execute("SELECT COUNT(*) FROM workbench_report").fetchone()[0] == 0
    store.close()


def test_atomic_completion_rolls_back_report_on_failure_and_saves_exactly_once(tmp_path):
    store, matter = setup_store(tmp_path / "control.sqlite")
    jobs = ReportCompilationJobs(store)
    first = queue(jobs, matter)
    claimed = jobs.claim("synthetic-worker")
    jobs.record_input_fingerprint(claimed, FINGERPRINT)

    def broken_builder():
        build_report(store, matter)
        raise RuntimeError("Synthetic interrupted transaction")

    with pytest.raises(RuntimeError):
        jobs.complete(claimed, broken_builder, fingerprint=FINGERPRINT)
    assert jobs.get(matter.matter_id, ACTOR, first.job_id).state == "running"
    assert store.connection.execute("SELECT COUNT(*) FROM workbench_report").fetchone()[0] == 0
    report = jobs.complete(claimed, lambda: build_report(store, matter), fingerprint=FINGERPRINT)
    done = jobs.get(matter.matter_id, ACTOR, first.job_id)
    assert done.state == "succeeded" and done.report_id == report
    with pytest.raises(CompilationLeaseLost):
        jobs.complete(claimed, lambda: build_report(store, matter, "report-synthetic-duplicate"), fingerprint=FINGERPRINT)
    assert store.connection.execute("SELECT COUNT(*) FROM workbench_report").fetchone()[0] == 1
    store.close()


def test_cancel_and_changed_fingerprint_prevent_report_commit(tmp_path):
    store, matter = setup_store(tmp_path / "control.sqlite")
    jobs = ReportCompilationJobs(store)
    queued = queue(jobs, matter)
    claimed = jobs.claim("synthetic-worker")
    jobs.record_input_fingerprint(claimed, FINGERPRINT)
    with pytest.raises(CompilationProblem, match="inputs changed"):
        jobs.complete(claimed, lambda: build_report(store, matter), fingerprint="b" * 64)
    jobs.cancel(matter.matter_id, ACTOR, queued.job_id)
    assert jobs.cancelled(claimed)
    with pytest.raises(CompilationLeaseLost):
        jobs.complete(claimed, lambda: build_report(store, matter), fingerprint=FINGERPRINT)
    jobs.fail(claimed)
    assert jobs.get(matter.matter_id, ACTOR, queued.job_id).state == "cancelled"
    retried = jobs.retry(matter.matter_id, ACTOR, queued.job_id)
    assert retried.state == "queued" and retried.input_fingerprint == ""
    assert store.connection.execute("SELECT COUNT(*) FROM workbench_report").fetchone()[0] == 0
    store.close()


@pytest.mark.parametrize("change", ["revoked", "purging"])
def test_closed_matter_or_revoked_membership_cannot_queue_claim_or_finish(tmp_path, change):
    store, matter = setup_store(tmp_path / "control.sqlite")
    jobs = ReportCompilationJobs(store)
    queued = queue(jobs, matter)
    claimed = jobs.claim("synthetic-worker")
    jobs.record_input_fingerprint(claimed, FINGERPRINT)
    with fixture_write(store):
        if change == "revoked":
            store.connection.execute("UPDATE workbench_matter_membership SET state='revoked' WHERE matter_id=? AND principal_id=?", (matter.matter_id, ACTOR))
        else:
            store.connection.execute("UPDATE workbench_matter_lifecycle SET state='purging' WHERE matter_id=?", (matter.matter_id,))
    with pytest.raises(CompilationLeaseLost):
        jobs.complete(claimed, lambda: build_report(store, matter), fingerprint=FINGERPRINT)
    with pytest.raises(KeyError):
        queue(jobs, matter, "b")
    assert jobs.claim("another-worker") is None
    assert store.connection.execute("SELECT state FROM workbench_report_compilation_job WHERE job_id=?", (queued.job_id,)).fetchone()[0] == ("failed" if change == "revoked" else "running")
    store.close()


def test_admission_limits_apply_atomically_and_retry_rechecks_capacity(tmp_path):
    store, matter = setup_store(tmp_path / "control.sqlite")
    jobs = ReportCompilationJobs(store, policy=CompilationJobPolicy(actor_active_limit=1))
    first = queue(jobs, matter)
    with pytest.raises(CompilationProblem, match="queue is full"):
        queue(jobs, matter, "b")
    jobs.cancel(matter.matter_id, ACTOR, first.job_id)
    queue(jobs, matter, "b")
    with pytest.raises(CompilationProblem, match="queue is full"):
        jobs.retry(matter.matter_id, ACTOR, first.job_id)
    store.close()


@pytest.mark.parametrize("lifecycle", ["closing", "purging", "purge_failed", "deleted"])
@pytest.mark.parametrize("job_state", ["queued", "running"])
def test_frozen_jobs_remain_unchanged_and_do_not_block_active_work(tmp_path, lifecycle, job_state):
    store, frozen = setup_store(tmp_path / "control.sqlite")
    now = [100.0]
    jobs = ReportCompilationJobs(store, policy=CompilationJobPolicy(
        pending_limit=1, actor_active_limit=1, lease_seconds=10), clock=lambda: now[0])
    queued = queue(jobs, frozen)
    claimed = jobs.claim("synthetic-old-worker") if job_state == "running" else None
    with fixture_write(store):
        store.connection.execute("UPDATE workbench_matter_lifecycle SET state=? WHERE matter_id=?",
                                 (lifecycle, frozen.matter_id))
    before = tuple(store.connection.execute(
        "SELECT * FROM workbench_report_compilation_job WHERE job_id=?", (queued.job_id,)).fetchone())
    events_before = store.audit_events(frozen.matter_id)
    active = store.create_matter("Generated active matter", "Synthetic", ACTOR)
    active_job = queue(jobs, active, "b")
    now[0] = 111.0
    jobs.recover_expired()
    assert jobs.claim("synthetic-active-worker").job_id == active_job.job_id
    assert jobs.claim("synthetic-later-worker") is None
    if claimed is not None:
        assert jobs.heartbeat(claimed) is False
    assert tuple(store.connection.execute(
        "SELECT * FROM workbench_report_compilation_job WHERE job_id=?", (queued.job_id,)).fetchone()) == before
    assert store.audit_events(frozen.matter_id) == events_before
    store.close()


def test_online_backup_and_clean_restore_preserve_lease_and_snapshot_basis(tmp_path):
    original = tmp_path / "original.sqlite"
    restored_path = tmp_path / "restored.sqlite"
    store, matter = setup_store(original)
    clock = [100.0]
    jobs = ReportCompilationJobs(store, policy=CompilationJobPolicy(lease_seconds=10), clock=lambda: clock[0])
    first = queue(jobs, matter)
    claimed = jobs.claim("synthetic-worker")
    jobs.record_input_fingerprint(claimed, FINGERPRINT)
    backup = sqlite3.connect(restored_path)
    store.connection.backup(backup)
    backup.close()
    store.close()
    restored = WorkspaceStore(restored_path)
    durable = ReportCompilationJobs(restored, policy=CompilationJobPolicy(lease_seconds=10), clock=lambda: clock[0])
    saved = durable.get(matter.matter_id, ACTOR, first.job_id)
    assert saved.input_fingerprint == FINGERPRINT and saved.lease_token == claimed.lease_token
    assert durable.claim("synthetic-restored-worker") is None
    clock[0] = 111.0
    recovered = durable.claim("synthetic-restored-worker")
    assert recovered.job_id == first.job_id and recovered.attempts == 2
    assert recovered.selections == first.selections
    assert restored.connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    restored.close()


def test_coordinator_runs_outside_caller_and_completes_transactionally(tmp_path, monkeypatch):
    store, matter = setup_store(tmp_path / "control.sqlite")
    now = [100.0]
    jobs = ReportCompilationJobs(store, policy=CompilationJobPolicy(lease_seconds=0.3), clock=lambda: now[0])
    queued = queue(jobs, matter)
    entered = threading.Event()
    release = threading.Event()
    renewed = threading.Condition()
    original_heartbeat = jobs.heartbeat

    def heartbeat(job):
        result = original_heartbeat(job)
        with renewed:
            renewed.notify_all()
        return result

    monkeypatch.setattr(jobs, "heartbeat", heartbeat)

    def lease_was_extended():
        active = jobs.get(matter.matter_id, ACTOR, queued.job_id)
        return (active.state == "running" and active.lease_expires_at is not None
                and active.lease_expires_at >= now[0] + jobs.policy.lease_seconds)

    def process(job, cancelled):
        jobs.record_input_fingerprint(job, FINGERPRINT)
        entered.set()
        assert release.wait(5)
        assert not cancelled()
        return "synthetic-draft"

    def finish(job, draft):
        assert draft == "synthetic-draft"
        return jobs.complete(job, lambda: build_report(store, matter), fingerprint=FINGERPRINT)

    coordinator = ReportCompilationCoordinator(jobs, process=process, finish=finish)
    try:
        assert entered.wait(5)
        original = jobs.get(matter.matter_id, ACTOR, queued.job_id)
        # Advance only after observing each committed renewal. Runner scheduling
        # affects the bounded wait, never whether the synthetic lease expires.
        for _ in range(3):
            with renewed:
                now[0] += jobs.policy.lease_seconds / 2
                assert renewed.wait_for(lease_was_extended, timeout=5), "Heartbeat did not extend the durable lease."
            assert jobs.claim("synthetic-competing-worker") is None
            active = jobs.get(matter.matter_id, ACTOR, queued.job_id)
            assert active.attempts == 1 and active.lease_token == original.lease_token
        assert now[0] > original.lease_expires_at
        release.set()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and jobs.get(matter.matter_id, ACTOR, queued.job_id).state == "running":
            time.sleep(0.01)
        assert jobs.get(matter.matter_id, ACTOR, queued.job_id).state == "succeeded"
    finally:
        release.set()
        coordinator.close()
        store.close()


def test_shutdown_fences_inflight_work_and_requeues_its_intent(tmp_path):
    store, matter = setup_store(tmp_path / "control.sqlite")
    jobs = ReportCompilationJobs(store)
    queued = queue(jobs, matter)
    entered = threading.Event()

    def process(job, cancelled):
        entered.set()
        while not cancelled():
            time.sleep(0.01)
        return "discarded-draft"

    coordinator = ReportCompilationCoordinator(jobs, process=process, finish=lambda *_: pytest.fail("A stopped worker must not save."))
    assert entered.wait(5)
    coordinator.close()
    assert jobs.get(matter.matter_id, ACTOR, queued.job_id).state == "queued"
    assert store.connection.execute("SELECT COUNT(*) FROM workbench_report").fetchone()[0] == 0
    store.close()


def test_repeated_expiry_requires_explicit_retry_after_attempt_budget(tmp_path):
    store, matter = setup_store(tmp_path / "control.sqlite")
    clock = [100.0]
    jobs = ReportCompilationJobs(store, policy=CompilationJobPolicy(lease_seconds=1, maximum_attempts=2), clock=lambda: clock[0])
    queued = queue(jobs, matter)
    assert jobs.claim("first-worker").attempts == 1
    clock[0] += 2
    assert jobs.claim("second-worker").attempts == 2
    clock[0] += 2
    assert jobs.claim("third-worker") is None
    assert jobs.get(matter.matter_id, ACTOR, queued.job_id).state == "failed"
    assert jobs.retry(matter.matter_id, ACTOR, queued.job_id).attempts == 0
    assert jobs.claim("third-worker").attempts == 1
    store.close()


def test_lease_expiring_during_builder_rolls_back_created_report(tmp_path):
    store, matter = setup_store(tmp_path / "control.sqlite")
    clock = [100.0]
    jobs = ReportCompilationJobs(store, policy=CompilationJobPolicy(lease_seconds=1), clock=lambda: clock[0])
    queue(jobs, matter)
    claimed = jobs.claim("worker")
    jobs.record_input_fingerprint(claimed, FINGERPRINT)

    def delayed_builder():
        report = build_report(store, matter)
        clock[0] += 2
        return report

    with pytest.raises(CompilationLeaseLost):
        jobs.complete(claimed, delayed_builder, fingerprint=FINGERPRINT)
    assert store.connection.execute("SELECT COUNT(*) FROM workbench_report").fetchone()[0] == 0
    store.close()


def test_late_previous_heartbeat_cannot_cancel_the_next_job(tmp_path, monkeypatch):
    store, matter = setup_store(tmp_path / "control.sqlite")
    now = [100.0]
    jobs = ReportCompilationJobs(store, policy=CompilationJobPolicy(lease_seconds=0.3), clock=lambda: now[0])
    first = queue(jobs, matter)
    now[0] += 0.01  # Preserve queue order without relying on wall-clock timestamps.
    second = queue(jobs, matter, "b")
    heartbeat_entered = threading.Event()
    release_heartbeat = threading.Event()
    second_entered = threading.Event()
    release_second = threading.Event()
    original = jobs.heartbeat
    previous_heartbeat = []

    def delayed_heartbeat(job):
        if job.job_id == first.job_id:
            previous_heartbeat.append(threading.current_thread())
            heartbeat_entered.set()
            assert release_heartbeat.wait(5)
        return original(job)

    monkeypatch.setattr(jobs, "heartbeat", delayed_heartbeat)

    def process(job, cancelled):
        jobs.record_input_fingerprint(job, FINGERPRINT)
        if job.job_id == first.job_id:
            assert heartbeat_entered.wait(5)
        else:
            second_entered.set()
            assert release_second.wait(5)
            assert not cancelled()
        return job.job_id

    def finish(job, draft):
        return jobs.complete(job, lambda: build_report(store, matter, "report-" + draft), fingerprint=FINGERPRINT)

    coordinator = ReportCompilationCoordinator(jobs, process=process, finish=finish)
    try:
        assert second_entered.wait(5)
        release_heartbeat.set()
        previous_heartbeat[0].join(timeout=5)
        assert not previous_heartbeat[0].is_alive()
        release_second.set()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and jobs.get(matter.matter_id, ACTOR, second.job_id).state == "running":
            time.sleep(0.01)
        assert jobs.get(matter.matter_id, ACTOR, second.job_id).state == "succeeded"
    finally:
        release_heartbeat.set()
        release_second.set()
        coordinator.close()
        store.close()


@pytest.mark.parametrize("validation_phase", ["snapshot", "final"])
def test_locked_source_validation_outlasting_lease_keeps_worker_owned(tmp_path, validation_phase):
    path = tmp_path / "control.sqlite"
    store, matter = setup_store(path)
    competing_store = WorkspaceStore(path)
    policy = CompilationJobPolicy(lease_seconds=0.6)
    now = [100.0]
    jobs = ReportCompilationJobs(store, policy=policy, clock=lambda: now[0])
    competitor = ReportCompilationJobs(competing_store, policy=policy, clock=lambda: now[0])
    queued = queue(jobs, matter)
    validating = threading.Event()
    release = threading.Event()

    def validate_sources():
        # Snapshot and final citation validation hold this lock but do not open
        # a write transaction. Another process must see live lease renewals.
        with store._lock:
            validating.set()
            assert release.wait(20)

    def process(job, cancelled):
        if validation_phase == "snapshot":
            validate_sources()
        jobs.record_input_fingerprint(job, FINGERPRINT)
        assert not cancelled()
        return "synthetic-draft"

    def finish(job, draft):
        if validation_phase == "final":
            validate_sources()
        return jobs.complete(job, lambda: build_report(store, matter), fingerprint=FINGERPRINT)

    coordinator = ReportCompilationCoordinator(jobs, process=process, finish=finish)
    try:
        assert validating.wait(5)
        original = competitor.get(matter.matter_id, ACTOR, queued.job_id)
        # Advance lease time in bounded steps, observing each committed renewal
        # through the independent connection. Thread scheduling must not accidentally
        # expire a 0.6-second lease and turn this lock-isolation test into a test
        # of CI runner load. The coordinator and heartbeat writes remain real.
        for _ in range(3):
            now[0] += policy.lease_seconds / 2
            deadline = time.monotonic() + 5
            while True:
                active = competitor.get(matter.matter_id, ACTOR, queued.job_id)
                assert active.state == "running" and active.lease_expires_at is not None
                if active.lease_expires_at >= now[0] + policy.lease_seconds:
                    break
                assert time.monotonic() < deadline, "Heartbeat did not renew during locked validation."
                time.sleep(0.01)
            assert not release.is_set()
            assert competitor.claim("synthetic-competing-worker") is None
            assert active.state == "running" and active.attempts == 1
            assert active.lease_token == original.lease_token
        assert now[0] > original.lease_expires_at
        release.set()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and competitor.get(matter.matter_id, ACTOR, queued.job_id).state == "running":
            time.sleep(0.01)
        assert competitor.get(matter.matter_id, ACTOR, queued.job_id).state == "succeeded"
        assert competing_store.connection.execute("SELECT COUNT(*) FROM workbench_report").fetchone()[0] == 1
    finally:
        release.set()
        coordinator.close()
        competing_store.close()
        store.close()


@pytest.mark.parametrize("reason", ["cancelled", "revoked"])
def test_independent_heartbeat_does_not_renew_cancelled_or_revoked_work(tmp_path, reason):
    store, matter = setup_store(tmp_path / "control.sqlite")
    jobs = ReportCompilationJobs(store)
    queued = queue(jobs, matter)
    claimed = jobs.claim("synthetic-worker")
    before = claimed.lease_expires_at
    if reason == "cancelled":
        jobs.cancel(matter.matter_id, ACTOR, queued.job_id)
    else:
        with fixture_write(store):
            store.connection.execute("UPDATE workbench_matter_membership SET state='revoked' WHERE matter_id=? AND principal_id=?", (matter.matter_id, ACTOR))
    assert jobs.heartbeat(claimed) is False
    assert store.connection.execute("SELECT lease_expires_at FROM workbench_report_compilation_job WHERE job_id=?", (queued.job_id,)).fetchone()[0] == before
    store.close()


@pytest.mark.parametrize("reason", ["attempt_limit", "cancelled", "revoked_running", "revoked_queued", "inactive_principal", "cancelled_and_revoked"])
def test_recovery_terminal_transitions_append_one_attributed_content_free_audit(tmp_path, reason):
    store, matter = setup_store(tmp_path / "control.sqlite")
    now = [100.0]
    jobs = ReportCompilationJobs(store, policy=CompilationJobPolicy(lease_seconds=10, maximum_attempts=1), clock=lambda: now[0])
    queued = queue(jobs, matter)
    if reason != "revoked_queued":
        jobs.claim("synthetic-worker")
    if reason in {"cancelled", "cancelled_and_revoked"}:
        jobs.cancel(matter.matter_id, ACTOR, queued.job_id)
    with fixture_write(store):
        if reason in {"revoked_running", "revoked_queued", "cancelled_and_revoked"}:
            store.connection.execute("UPDATE workbench_matter_membership SET state='revoked' WHERE matter_id=? AND principal_id=?", (matter.matter_id, ACTOR))
        elif reason == "inactive_principal":
            store.connection.execute("UPDATE workbench_principal SET active=0 WHERE principal_id=?", (ACTOR,))
    if reason in {"attempt_limit", "cancelled", "cancelled_and_revoked"}:
        now[0] = 111.0
    jobs.recover_expired()
    expected = "cancelled" if reason in {"cancelled", "cancelled_and_revoked"} else "failed"
    assert store.connection.execute("SELECT state FROM workbench_report_compilation_job WHERE job_id=?", (queued.job_id,)).fetchone()[0] == expected
    events = [event for event in store.audit_events(matter.matter_id) if event.action == "report.compile.recover"]
    assert len(events) == 1
    event = events[0]
    assert (event.actor_principal_id, event.matter_id, event.object_type, event.object_id, event.request_id) == (ACTOR, matter.matter_id, "report_compilation", queued.job_id, queued.job_id)
    assert event.outcome == "failure" and event.details == {"state": expected}
    jobs.recover_expired()
    assert jobs.claim("synthetic-later-worker") is None
    assert [event for event in store.audit_events(matter.matter_id) if event.action == "report.compile.recover"] == events
    store.close()


def test_recovery_requeue_does_not_emit_a_terminal_audit(tmp_path):
    store, matter = setup_store(tmp_path / "control.sqlite")
    now = [100.0]
    jobs = ReportCompilationJobs(store, policy=CompilationJobPolicy(lease_seconds=10), clock=lambda: now[0])
    queued = queue(jobs, matter)
    jobs.claim("synthetic-first-worker")
    now[0] = 111.0
    jobs.recover_expired()
    assert jobs.get(matter.matter_id, ACTOR, queued.job_id).state == "queued"
    assert not [event for event in store.audit_events(matter.matter_id) if event.action == "report.compile.recover"]
    store.close()


def test_recovery_state_and_audit_roll_back_together_on_audit_failure(tmp_path, monkeypatch):
    store, matter = setup_store(tmp_path / "control.sqlite")
    now = [100.0]
    jobs = ReportCompilationJobs(store, policy=CompilationJobPolicy(lease_seconds=10, maximum_attempts=1), clock=lambda: now[0])
    queued = queue(jobs, matter)
    jobs.claim("synthetic-worker")
    now[0] = 111.0
    append = store._append_audit_event_locked
    def fail_audit(**kwargs):
        append(**kwargs)
        raise RuntimeError("Synthetic interrupted audit")
    monkeypatch.setattr(store, "_append_audit_event_locked", fail_audit)
    with pytest.raises(RuntimeError, match="Synthetic interrupted audit"):
        jobs.recover_expired()
    assert jobs.get(matter.matter_id, ACTOR, queued.job_id).state == "running"
    assert not [event for event in store.audit_events(matter.matter_id) if event.action == "report.compile.recover"]
    monkeypatch.setattr(store, "_append_audit_event_locked", append)
    jobs.recover_expired()
    assert jobs.get(matter.matter_id, ACTOR, queued.job_id).state == "failed"
    assert len([event for event in store.audit_events(matter.matter_id) if event.action == "report.compile.recover"]) == 1
    store.close()


def test_actionable_list_filters_successes_before_limit_and_retains_deleted_results(tmp_path):
    store, matter = setup_store(tmp_path / "control.sqlite")
    clock = [100.0]
    jobs = ReportCompilationJobs(store, clock=lambda: clock[0])
    deleted = queue(jobs, matter, "d")
    claimed = jobs.claim("synthetic-worker")
    jobs.record_input_fingerprint(claimed, FINGERPRINT)
    jobs.complete(claimed, lambda: build_report(store, matter, "report-" + "d" * 32), fingerprint=FINGERPRINT)
    report = store.reports(matter.matter_id, ACTOR)[0]
    store.delete_report(matter.matter_id, report.report_id, ACTOR, expected_updated_at=report.updated_at)
    for index in range(3):
        clock[0] += 1
        queue(jobs, matter, str(index))
        claimed = jobs.claim("synthetic-worker")
        jobs.record_input_fingerprint(claimed, FINGERPRINT)
        jobs.complete(claimed, lambda: build_report(store, matter, f"report-synthetic-{index}"), fingerprint=FINGERPRINT)
    assert jobs.list(matter.matter_id, ACTOR, limit=1)[0].report_id
    actionable = jobs.list(matter.matter_id, ACTOR, limit=1, actionable_only=True)
    assert len(actionable) == 1 and actionable[0].job_id == deleted.job_id and not actionable[0].report_id
    store.close()


def test_shutdown_after_cancel_records_terminal_message_and_time(tmp_path):
    store, matter = setup_store(tmp_path / "control.sqlite")
    jobs = ReportCompilationJobs(store)
    queued = queue(jobs, matter)
    claimed = jobs.claim("synthetic-worker")
    pending = jobs.cancel(matter.matter_id, ACTOR, queued.job_id)
    assert pending.state == "running" and pending.message == "Cancelling compilation."
    jobs.release(claimed)
    terminal = jobs.get(matter.matter_id, ACTOR, queued.job_id)
    assert terminal.state == "cancelled" and terminal.message == "Compilation cancelled."
    assert terminal.finished_at is not None
    store.close()


@pytest.mark.parametrize('expire', [False, True])
def test_busy_heartbeat_retries_until_renewal_or_actual_lease_expiry(tmp_path, monkeypatch, expire):
    store, matter = setup_store(tmp_path / 'synthetic-contention.sqlite')
    now = [100.0]
    jobs = ReportCompilationJobs(store, policy=CompilationJobPolicy(lease_seconds=1.2), clock=lambda: now[0])
    queued = queue(jobs, matter)
    processing = threading.Event()
    release = threading.Event()
    busy = threading.Event()
    renewed = threading.Event()
    cancellation_seen = threading.Event()
    observed = {}
    original = jobs.heartbeat
    def heartbeat(job):
        try:
            result = original(job)
        except sqlite3.OperationalError:
            busy.set()
            raise
        if busy.is_set() and result:
            renewed.set()
        return result
    monkeypatch.setattr(jobs, 'heartbeat', heartbeat)
    def process(job, cancelled):
        jobs.record_input_fingerprint(job, FINGERPRINT)
        observed['job'] = job
        processing.set()
        if expire:
            while not cancelled():
                time.sleep(0.01)
            observed['cancelled_at'] = jobs.clock()
            cancellation_seen.set()
            assert release.wait(5)
            raise CompilationLeaseLost('Synthetic expired lease.')
        assert release.wait(5)
        assert not cancelled()
        return 'synthetic-draft'
    def finish(job, draft):
        return jobs.complete(job, lambda: build_report(store, matter), fingerprint=FINGERPRINT)
    coordinator = ReportCompilationCoordinator(jobs, process=process, finish=finish)
    writer = sqlite3.connect(store.path, timeout=1)
    try:
        assert processing.wait(5)
        # fixture-write: Independent writer must block real heartbeat renewal to exercise contention.
        writer.execute('BEGIN IMMEDIATE')
        assert busy.wait(3)  # Real independent SQLite writer outlasts busy_timeout.
        # The durable deadline can include renewals before the writer acquired
        # its lock. Hold lease time still until the intended contention exists.
        lease_deadline = jobs.heartbeat_deadline(observed['job'])
        assert lease_deadline is not None and jobs.clock() < lease_deadline
        assert not cancellation_seen.is_set()
        if expire:
            now[0] = lease_deadline
            assert cancellation_seen.wait(3)
            assert observed['cancelled_at'] >= lease_deadline
            assert not renewed.is_set()
            coordinator._stop.set()
            writer.rollback()
            assert jobs.heartbeat(observed['job']) is False
            with pytest.raises(CompilationLeaseLost):
                jobs.complete(observed['job'], lambda: build_report(store, matter), fingerprint=FINGERPRINT)
            assert store.connection.execute('SELECT count(*) FROM workbench_report').fetchone()[0] == 0
            release.set()
        else:
            now[0] = lease_deadline - jobs.policy.lease_seconds / 2
            assert jobs.clock() < lease_deadline
            writer.rollback()
            assert renewed.wait(3)
            active = jobs.get(matter.matter_id, ACTOR, queued.job_id)
            assert active.lease_expires_at > lease_deadline
            assert active.attempts == 1 and active.lease_token == observed['job'].lease_token
            release.set()
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and jobs.get(matter.matter_id, ACTOR, queued.job_id).state == 'running':
                time.sleep(0.01)
            assert jobs.get(matter.matter_id, ACTOR, queued.job_id).state == 'succeeded'
            assert store.connection.execute('SELECT count(*) FROM workbench_report').fetchone()[0] == 1
    finally:
        writer.rollback()
        writer.close()
        release.set()
        coordinator.close()
        store.close()
