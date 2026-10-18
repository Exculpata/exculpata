"""Security regressions at public retry/export and durable-job boundaries."""

from tests.fixture_write import fixture_write

from tests.case_lifecycle_helpers import lifecycle_session
from copy import deepcopy
from dataclasses import replace
import json
import re

import pytest

from tests.case_lifecycle_web_helpers import claim_bench_exit

from case_intelligence.workspace_store import WorkspaceProblem
from case_intelligence.full_text_review import FullTextReviewLedger
from case_intelligence.work_product_exports import ExportProblem
from case_intelligence.report_review_basis import research_sections
from case_intelligence.report_materials import snapshot_report_materials
from tests.test_full_text_synthesis import ACTOR, finish, queue, seed_terminal_review, workspace


def csrf(client, matter, job):
    response = client.get(f"/matters/{matter.slug}/research?job={job.job_id}")
    assert response.status_code == 200
    return re.search(r'name="csrf_token" value="([^"]+)"', response.text).group(1)


@pytest.mark.parametrize("operation", ["set_plan", "restart", "progress", "fail", "checkpoint", "finish"])
@pytest.mark.parametrize("marker", ["valid", "missing", 0])
def test_legacy_job_mutations_cannot_bypass_full_text_fences(workspace, operation, marker):
    _, bench, matter, _ = workspace
    run, _, _ = seed_terminal_review(bench, matter, count=1)
    job = queue(bench, matter, run)
    # Research is stopped by the fixture, but other coordinators still share
    # this connection. Hold the workspace lock through the fixture commit.
    if marker != "valid":
        plan = dict(job.plan)
        if marker == "missing":
            plan.pop("full_text_synthesis_version")
        else:
            plan["full_text_synthesis_version"] = marker
        with fixture_write(bench.workspace):
            bench.workspace.connection.execute("UPDATE workbench_research_job SET plan_json=? WHERE job_id=?",
                (json.dumps(plan), job.job_id))
    before = bench.workspace.research_job(matter.matter_id, ACTOR, job.job_id)
    operations = {
        "set_plan": lambda: bench.workspace.set_research_plan(job.job_id, {}, 2),
        "restart": lambda: bench.workspace.restart_research_checkpoint(job.job_id, checkpoint={}),
        "progress": lambda: bench.workspace.update_research_progress(job.job_id, stage="searching", message="Synthetic stale worker", completed_steps=1, candidate_count=0, evidence_count=0),
        "fail": lambda: bench.workspace.fail_research_job(job.job_id, "Synthetic stale worker"),
        "checkpoint": lambda: bench.workspace.checkpoint_research_job(job.job_id, {}),
        "finish": lambda: bench.workspace.finish_research_job(job.job_id, {}),
    }
    with pytest.raises(WorkspaceProblem):
        operations[operation]()
    assert bench.workspace.research_job(matter.matter_id, ACTOR, job.job_id) == before


@pytest.mark.parametrize("reserved_key", ["zero_marker", "empty_receipt"])
def test_empty_adapter_keys_cannot_use_legacy_export_or_report_paths(workspace, reserved_key):
    _, bench, matter, _ = workspace
    run, _, _ = seed_terminal_review(bench, matter, count=1)
    completed = finish(bench, queue(bench, matter, run))
    plan, result = dict(completed.plan), dict(completed.result)
    if reserved_key == "zero_marker":
        plan["full_text_synthesis_version"] = 0
        result.pop("full_text_synthesis_input")
    else:
        plan.pop("full_text_synthesis_version")
        result["full_text_synthesis_input"] = {}
    corrupt = replace(completed, plan=plan, result=result)
    with pytest.raises(ExportProblem):
        bench.export_research_work_product(matter, corrupt, "json")
    with pytest.raises(WorkspaceProblem):
        research_sections(corrupt)


@pytest.mark.parametrize("marker", ["missing", 0, 2])
def test_queue_validates_adapter_binding_even_with_supplied_callback(workspace, marker):
    _, bench, matter, _ = workspace
    run, _, _ = seed_terminal_review(bench, matter, count=1)
    prepared = bench._full_text_synthesis_service(matter).prepare(matter.matter_id, ACTOR, run.run_id)
    receipt = prepared["full_text_synthesis_input"]
    plan = {"input_digest": receipt["input_digest"], "run_id": run.run_id}
    if marker != "missing":
        plan["full_text_synthesis_version"] = marker
    with pytest.raises(ValueError):
        bench.workspace.queue_research_job(matter.matter_id, ACTOR, receipt["question"], receipt["title"],
            "research-request-" + "a" * 32, initial_plan=plan, initial_result=prepared,
            validate_locked=lambda: None)
    assert not bench.workspace.research_jobs(matter.matter_id, ACTOR)


@pytest.mark.parametrize("marker", ["missing", 0, 2])
def test_invalid_version_preserves_charges_and_refuses_processing_retry_and_inspection(workspace, monkeypatch, marker):
    client, bench, matter, model = workspace
    run, _, statements = seed_terminal_review(bench, matter, count=2)
    job = queue(bench, matter, run)
    original = bench.generator.answer
    calls = []
    def interrupt(*args, **kwargs):
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError("Synthetic interruption after one saved issue")
        return original(*args, **kwargs)
    monkeypatch.setattr(bench.generator, "answer", interrupt)
    with pytest.raises(RuntimeError):
        bench._process_research_job(job, lambda: False)
    current = bench.workspace.research_job(matter.matter_id, ACTOR, job.job_id)
    saved = deepcopy(current.result["hierarchical_synthesis"])
    assert saved["requests_spent"] == 2 and len(saved["issue"]) == 1
    plan = dict(current.plan)
    if marker == "missing":
        plan.pop("full_text_synthesis_version")
    else:
        plan["full_text_synthesis_version"] = marker
    with fixture_write(bench.workspace):
        bench.workspace.connection.execute("UPDATE workbench_research_job SET plan_json=? WHERE job_id=?",
            (json.dumps(plan), job.job_id))
    corrupt = bench.workspace.research_job(matter.matter_id, ACTOR, job.job_id)
    # A worker claimed before the persisted marker changed also cannot save.
    for write in (bench.workspace.checkpoint_research_job, bench.workspace.finish_research_job):
        with pytest.raises(ValueError):
            write(job.job_id, current.result, expected_job=job, validate_locked=lambda: None)
        assert bench.workspace.research_job(matter.matter_id, ACTOR, job.job_id) == corrupt
    calls_before = len(model.calls)
    with pytest.raises(ValueError):
        bench._process_research_job(corrupt, lambda: False)
    assert len(model.calls) == calls_before
    # Coordinator failure must retain the attempt fence even for marker=0 or
    # receipt-only jobs, and preserve all charged work while recording failure.
    bench.research._fail(job.job_id, "Synthetic invalid version", expected_job=corrupt)
    failed = bench.workspace.research_job(matter.matter_id, ACTOR, job.job_id)
    assert failed.state == "failed" and failed.result["hierarchical_synthesis"] == saved
    assert bench._research_checkpoint_stale(matter, failed)
    response = client.get(f"/matters/{matter.slug}/research?job={job.job_id}")
    assert response.status_code == 200
    assert "Saved full-text findings" in response.text
    assert all(statement not in response.text for statement in statements)
    with pytest.raises(ValueError):
        bench.workspace.retry_research_job(matter.matter_id, ACTOR, job.job_id)
    with pytest.raises(ValueError):
        bench.workspace.retry_research_job(matter.matter_id, ACTOR, job.job_id,
            validate_locked=lambda current: None)
    response = client.post(f"/matters/{matter.slug}/research/{job.job_id}/retry",
        data={"csrf_token": csrf(client, matter, job)}, follow_redirects=False)
    assert response.status_code == 303 and "error=" in response.headers["location"]
    assert bench.workspace.research_job(matter.matter_id, ACTOR, job.job_id) == failed


def test_real_retry_route_preserves_intermediates_spend_and_fences_old_attempt(workspace, monkeypatch):
    client, bench, matter, _ = workspace
    run, _, _ = seed_terminal_review(bench, matter, count=12)
    job = queue(bench, matter, run)
    original = bench.generator.answer
    calls = []
    def interrupt(*args, **kwargs):
        calls.append(1)
        if len(calls) == 3:
            raise RuntimeError("Synthetic interruption after two saved intermediates")
        return original(*args, **kwargs)
    monkeypatch.setattr(bench.generator, "answer", interrupt)
    with pytest.raises(RuntimeError, match="Synthetic interruption"):
        bench._process_research_job(job, lambda: False)
    failed = bench.workspace.fail_research_job(job.job_id, "Synthetic interruption", expected_job=job)
    saved = deepcopy(failed.result["hierarchical_synthesis"])
    assert saved["requests_spent"] == 3 and len(saved["issue"]) == 2
    with pytest.raises(WorkspaceProblem):
        bench.workspace.retry_research_job(matter.matter_id, ACTOR, job.job_id)
    response = client.post(f"/matters/{matter.slug}/research/{job.job_id}/retry",
                           data={"csrf_token": csrf(client, matter, job)}, follow_redirects=False)
    assert response.status_code == 303 and "error=" not in response.headers["location"]
    resumed = bench.workspace.claim_research_job("synthetic-resumed-worker")
    assert resumed.plan == job.plan
    assert resumed.result["hierarchical_synthesis"] == saved
    assert resumed.result["full_text_synthesis_input"] == job.result["full_text_synthesis_input"]
    assert resumed.attempts == job.attempts + 1
    before = bench.workspace.research_job(matter.matter_id, ACTOR, job.job_id)
    with pytest.raises(WorkspaceProblem):
        bench.workspace.fail_research_job(job.job_id, "Synthetic late old worker failure", expected_job=job)
    with pytest.raises(WorkspaceProblem):
        bench.workspace.checkpoint_research_job(job.job_id, job.result, expected_job=job, validate_locked=lambda: None)
    with pytest.raises(WorkspaceProblem):
        bench.workspace.finish_research_job(job.job_id, job.result, expected_job=job, validate_locked=lambda: None)
    assert bench.workspace.research_job(matter.matter_id, ACTOR, job.job_id) == before
    monkeypatch.setattr(bench.generator, "answer", original)
    completed = finish(bench, resumed)
    hierarchy = completed.result["hierarchical_synthesis"]
    assert hierarchy["issue"][:2] == saved["issue"]
    assert hierarchy["requests_spent"] == 7
    assert hierarchy["started_at"] == saved["started_at"]


@pytest.mark.parametrize("change", ["membership", "source_set"])
def test_export_rechecks_actual_reader_and_source_scope_after_render(workspace, monkeypatch, change):
    client, bench, matter, _ = workspace
    run, documents, statements = seed_terminal_review(bench, matter, count=1, selected_set=True)
    job = finish(bench, queue(bench, matter, run))
    original = bench.export_research_work_product
    def render_then_revoke(*args, **kwargs):
        artifact = original(*args, **kwargs)
        if change == "membership":
            with fixture_write(bench.workspace):
                bench.workspace.connection.execute("UPDATE workbench_matter_membership SET state='revoked' WHERE matter_id=?", (matter.matter_id,))
        else:
            bench.workspace.remove_source_organization(matter.matter_id, documents[0].document_id)
        return artifact
    monkeypatch.setattr(bench, "export_research_work_product", render_then_revoke)
    response = client.get(f"/matters/{matter.slug}/research/{job.job_id}/export?format=json")
    assert response.status_code in {404, 409}
    assert statements[0] not in response.text


def test_quarantined_frozen_export_refuses_without_actual_original_verification(workspace):
    _, bench, matter, _ = workspace
    run, _, _ = seed_terminal_review(bench, matter, count=2, extra_outcomes=True)
    job = finish(bench, queue(bench, matter, run))
    catalog = bench.workspace.source_catalog_for_export(matter.matter_id, ACTOR)
    with pytest.raises(ExportProblem):
        bench.export_research_work_product(matter, job, "json", frozen_source_catalog=catalog)


def test_review_deletion_before_admission_refuses_without_creating_job(workspace, monkeypatch):
    _, bench, matter, _ = workspace
    run, _, _ = seed_terminal_review(bench, matter, count=1)
    original = bench.workspace.queue_research_job
    def delete_then_admit(*args, **kwargs):
        FullTextReviewLedger(bench.workspace).delete(matter.matter_id, ACTOR, run.run_id)
        return original(*args, **kwargs)
    monkeypatch.setattr(bench.workspace, "queue_research_job", delete_then_admit)
    with pytest.raises(WorkspaceProblem):
        bench.queue_full_text_synthesis(matter, ACTOR, run.run_id, "research-request-" + "a" * 32)
    assert not bench.workspace.research_jobs(matter.matter_id, ACTOR)


def test_review_deletion_during_generation_keeps_charge_but_saves_no_node(workspace, monkeypatch):
    _, bench, matter, _ = workspace
    run, _, _ = seed_terminal_review(bench, matter, count=1)
    job = queue(bench, matter, run)
    original = bench.generator.answer
    def answer_then_delete(*args, **kwargs):
        result = original(*args, **kwargs)
        FullTextReviewLedger(bench.workspace).delete(matter.matter_id, ACTOR, run.run_id)
        return result
    monkeypatch.setattr(bench.generator, "answer", answer_then_delete)
    with pytest.raises(WorkspaceProblem):
        bench._process_research_job(job, lambda: False)
    saved = bench.workspace.research_job(matter.matter_id, ACTOR, job.job_id).result["hierarchical_synthesis"]
    assert saved["requests_spent"] == 1 and saved["issue"] == []


@pytest.mark.parametrize("submitted", [None, "invalid-synthetic-token"])
def test_synthesis_creation_requires_csrf_even_with_valid_snapshot(workspace, monkeypatch, submitted):
    client, bench, matter, _ = workspace
    identity = client.app.state.identity
    validate = identity.csrf_valid
    # Test authentication normally bypasses CSRF. Exercise the production
    # validator while retaining this fixture's synthetic authenticated actor.
    monkeypatch.setattr(identity, "csrf_valid", lambda context, submitted:
        validate(replace(context, auth_method="local"), submitted))
    run, _, _ = seed_terminal_review(bench, matter, count=1)
    receipt = bench._full_text_synthesis_service(matter).repository.capture(matter.matter_id, ACTOR, run.run_id)["receipt"]
    response = client.post(f"/matters/{matter.slug}/full-review/{run.run_id}/synthesize",
        data={"request_key": "research-request-" + "a" * 32, "expected_snapshot": receipt["snapshot_digest"],
              **({"csrf_token": submitted} if submitted is not None else {})},
        follow_redirects=False)
    assert response.status_code == 403
    assert not bench.workspace.research_jobs(matter.matter_id, ACTOR)


def test_active_synthesis_blocks_matter_purge_and_late_worker_cannot_recreate_purged_state(workspace):
    _, bench, matter, _ = workspace
    run, _, _ = seed_terminal_review(bench, matter, count=1)
    job = queue(bench, matter, run)
    with pytest.raises(WorkspaceProblem, match="Wait for current"):
        bench.workspace.case_lifecycle.begin_exit(matter.slug, ACTOR, session_id=lifecycle_session(bench.workspace, ACTOR, administrator_override=False).session_id)
    bench.workspace.fail_research_job(job.job_id, "Synthetic stopped worker", expected_job=job)
    _, lifecycle = claim_bench_exit(bench, matter, ACTOR)
    bench.workspace.complete_matter_purge(matter.matter_id, lifecycle.purge_id)
    for operation in (
        lambda: bench.workspace.fail_research_job(job.job_id, "Synthetic late failure", expected_job=job),
        lambda: bench.workspace.checkpoint_research_job(job.job_id, job.result, expected_job=job, validate_locked=lambda: None),
        lambda: bench.workspace.finish_research_job(job.job_id, job.result, expected_job=job, validate_locked=lambda: None),
    ):
        with pytest.raises((WorkspaceProblem, KeyError)):
            operation()
    for table in ("workbench_research_job", "workbench_review_run", "workbench_review_decision"):
        assert bench.workspace.connection.execute(f"SELECT count(*) FROM {table} WHERE matter_id=?", (matter.matter_id,)).fetchone()[0] == 0


@pytest.mark.parametrize("changed", ["input_digest", "run_id", "question", "matter_id", "source_set_id"])
def test_interrupted_checkpoint_refuses_mismatched_job_binding(workspace, monkeypatch, changed):
    client, bench, matter, _ = workspace
    run, _, statements = seed_terminal_review(bench, matter, count=2)
    job = queue(bench, matter, run)
    original = bench.generator.answer
    calls = []
    def interrupt(*args, **kwargs):
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError("Synthetic interruption after one saved issue")
        return original(*args, **kwargs)
    monkeypatch.setattr(bench.generator, "answer", interrupt)
    with pytest.raises(RuntimeError):
        bench._process_research_job(job, lambda: False)
    failed = bench.workspace.fail_research_job(job.job_id, "Synthetic interruption", expected_job=job)
    assert len(failed.result["hierarchical_synthesis"]["issue"]) == 1
    if changed in {"input_digest", "run_id"}:
        bad = replace(failed, plan={**failed.plan, changed: "0" * 64})
    else:
        bad = replace(failed, **{changed: "synthetic-mismatched-binding"})
    assert bench._research_checkpoint_stale(matter, bad)
    # Exercise the real inspector for a plan-only mismatch without changing
    # matter routing or permission records as part of the corruption fixture.
    with fixture_write(bench.workspace):
        bench.workspace.connection.execute("UPDATE workbench_research_job SET plan_json=? WHERE job_id=?",
            (json.dumps({**failed.plan, "input_digest": "0" * 64}), job.job_id))
    response = client.get(f"/matters/{matter.slug}/research?job={job.job_id}")
    assert response.status_code == 200
    assert statements[0] not in response.text and statements[-1] not in response.text


@pytest.mark.parametrize("detection", ["both", "marker_only", "receipt_only"])
def test_full_text_synthesis_report_converters_refuse_before_copy_or_generation(workspace, monkeypatch, detection):
    _, bench, matter, model = workspace
    run, _, _ = seed_terminal_review(bench, matter, count=2)
    completed = finish(bench, queue(bench, matter, run))
    if detection == "marker_only":
        completed = replace(completed, result={key: value for key, value in completed.result.items() if key != "full_text_synthesis_input"})
    elif detection == "receipt_only":
        completed = replace(completed, plan={key: value for key, value in completed.plan.items() if key != "full_text_synthesis_version"})
    with pytest.raises(WorkspaceProblem, match="Copying it into a Report is not supported"):
        research_sections(completed)
    with fixture_write(bench.workspace):
        bench.workspace.connection.execute("UPDATE workbench_research_job SET plan_json=?,result_json=? WHERE job_id=?",
            (json.dumps(completed.plan), json.dumps(completed.result), completed.job_id))
    calls_before = len(model.calls)
    def no_generator(*args, **kwargs):
        raise AssertionError("Report generation must not start for an unsupported synthesis input")
    monkeypatch.setattr(bench.generator, "answer", no_generator)
    with bench.source_store(matter).mutation_guard(), bench.workspace._lock:
        with pytest.raises(WorkspaceProblem, match="Copying it into a Report is not supported"):
            snapshot_report_materials(bench, matter, ACTOR, (f"research:{completed.job_id}",))
    assert len(model.calls) == calls_before
    assert bench.workspace.reports(matter.matter_id, ACTOR) == ()


def test_report_choices_deep_link_and_direct_copy_refuse_full_text_synthesis(workspace):
    client, bench, matter, _ = workspace
    run, _, _ = seed_terminal_review(bench, matter, count=2)
    completed = finish(bench, queue(bench, matter, run))
    choices = client.get(f"/matters/{matter.slug}/reports/new")
    assert choices.status_code == 200
    assert f'value="research:{completed.job_id}"' not in choices.text
    deep_link = client.get(f"/matters/{matter.slug}/reports/new?from=research:{completed.job_id}")
    assert deep_link.status_code == 409
    assert "Copying it into a Report is not supported yet" in deep_link.text
    response = client.post(f"/matters/{matter.slug}/research/{completed.job_id}/report",
                           data={"csrf_token": csrf(client, matter, completed)}, follow_redirects=False)
    assert response.status_code == 303 and "error=" in response.headers["location"]
    assert bench.workspace.reports(matter.matter_id, ACTOR) == ()


@pytest.mark.parametrize("checkpoint_after_read", [1, 2])
def test_cancellation_cannot_overwrite_another_connections_charged_checkpoint(
        workspace, monkeypatch, checkpoint_after_read):
    import sqlite3
    from case_intelligence.full_text_synthesis_repository import FullTextSynthesisRepository
    from case_intelligence.workspace_store import WorkspaceStore

    _, bench, matter, _ = workspace
    run, _, _ = seed_terminal_review(bench, matter, count=8)
    job = queue(bench, matter, run)
    original_answer = bench.generator.answer
    calls = []

    def stop_after_issue(*args, **kwargs):
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError("Synthetic interruption after one saved issue")
        return original_answer(*args, **kwargs)

    monkeypatch.setattr(bench.generator, "answer", stop_after_issue)
    with pytest.raises(RuntimeError, match="Synthetic interruption"):
        bench._process_research_job(job, lambda: False)
    before = bench.workspace.research_job(matter.matter_id, ACTOR, job.job_id)
    assert before.result["hierarchical_synthesis"]["requests_spent"] == 2
    assert len(before.result["hierarchical_synthesis"]["issue"]) == 1
    next_result = deepcopy(before.result)
    next_result["hierarchical_synthesis"]["requests_spent"] = 3
    other = WorkspaceStore(bench.workspace.path)
    # An independent writer must either commit before cancellation's read or
    # wait until cancellation commits. Never wait for our own controlled hook.
    with other._lock:
        other.connection.execute("PRAGMA busy_timeout=0")  # fixture-write: Configure the independent contention writer to fail immediately instead of waiting on this test's other connection.
    repository = FullTextSynthesisRepository(connection=other.connection,
        lock=other._lock, authorize=other.membership)
    original_read = bench.workspace.research_job
    reads, committed = [], []

    def interleaved_read(*args, **kwargs):
        observed = original_read(*args, **kwargs)
        reads.append(1)
        if len(reads) == checkpoint_after_read:
            try:
                other.checkpoint_research_job(job.job_id, next_result,
                    expected_job=job, validate_locked=lambda: repository.validate_locked(
                        matter.matter_id, ACTOR, next_result["full_text_synthesis_input"]))
            except sqlite3.OperationalError as exc:
                assert "locked" in str(exc).lower()
            else:
                committed.append(True)
        return observed

    monkeypatch.setattr(bench.workspace, "research_job", interleaved_read)
    try:
        cancelled = bench.workspace.cancel_research_job(matter.matter_id, ACTOR, job.job_id)
        expected = next_result if committed else before.result
        assert cancelled.cancellation_requested
        assert cancelled.result == expected
        assert cancelled.result["hierarchical_synthesis"]["issue"] == before.result["hierarchical_synthesis"]["issue"]
        assert not bench.workspace.connection.in_transaction
        if checkpoint_after_read == 1:
            assert committed, "The earlier checkpoint should commit before cancellation takes its write lock"
    finally:
        other.close()


@pytest.mark.parametrize("checkpoint_timing", ["before_recovery", "after_snapshot"])
def test_restart_recovery_preserves_a_concurrent_charged_checkpoint(
        workspace, monkeypatch, checkpoint_timing):
    import sqlite3
    from case_intelligence.full_text_synthesis_repository import FullTextSynthesisRepository
    from case_intelligence.workspace_store import WorkspaceStore

    _, bench, matter, _ = workspace
    run, _, _ = seed_terminal_review(bench, matter, count=8)
    job = queue(bench, matter, run)
    original_answer = bench.generator.answer
    calls = []

    def stop_after_issue(*args, **kwargs):
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError("Synthetic interruption after one saved issue")
        return original_answer(*args, **kwargs)

    monkeypatch.setattr(bench.generator, "answer", stop_after_issue)
    with pytest.raises(RuntimeError, match="Synthetic interruption"):
        bench._process_research_job(job, lambda: False)
    before = bench.workspace.research_job(matter.matter_id, ACTOR, job.job_id)
    next_result = deepcopy(before.result)
    next_result["hierarchical_synthesis"]["requests_spent"] += 1
    other = WorkspaceStore(bench.workspace.path)
    with other._lock:
        other.connection.execute("PRAGMA busy_timeout=0")  # fixture-write: Configure the independent contention writer to fail immediately instead of waiting on this test's other connection.
    repository = FullTextSynthesisRepository(connection=other.connection,
        lock=other._lock, authorize=other.membership)
    committed = []

    def concurrent_checkpoint():
        try:
            other.checkpoint_research_job(job.job_id, next_result,
                expected_job=job, validate_locked=lambda: repository.validate_locked(
                    matter.matter_id, ACTOR, next_result["full_text_synthesis_input"]))
        except sqlite3.OperationalError as exc:
            assert "locked" in str(exc).lower()
        else:
            committed.append(True)

    class InterleavedCursor:
        def __init__(self, cursor):
            self.cursor = cursor

        def fetchall(self):
            rows = self.cursor.fetchall()
            concurrent_checkpoint()
            return rows

    class InterleavedConnection:
        def __init__(self, connection):
            self.connection = connection

        def __getattr__(self, name):
            return getattr(self.connection, name)

        def __enter__(self):
            self.connection.__enter__()
            return self

        def __exit__(self, *args):
            return self.connection.__exit__(*args)

        def execute(self, sql, *args):
            # fixture-write: This proxy observes product-owned transactions without changing their lifetime.
            cursor = self.connection.execute(sql, *args)
            if "FROM workbench_research_job WHERE state='running' ORDER BY" in sql:
                return InterleavedCursor(cursor)
            return cursor

    try:
        if checkpoint_timing == "before_recovery":
            concurrent_checkpoint()
            assert committed
        with monkeypatch.context() as patcher:
            if checkpoint_timing == "after_snapshot":
                patcher.setattr(bench.workspace, "connection", InterleavedConnection(bench.workspace.connection))
            assert bench.workspace.recover_running_research_jobs() == 1
        recovered = bench.workspace.research_job(matter.matter_id, ACTOR, job.job_id)
        assert recovered.state == "queued" and recovered.worker_id is None
        assert recovered.result == (next_result if committed else before.result)
        assert recovered.attempts == before.attempts
        assert not bench.workspace.connection.in_transaction
        with pytest.raises(WorkspaceProblem, match="no longer active"):
            other.checkpoint_research_job(job.job_id, next_result, expected_job=job,
                validate_locked=lambda: repository.validate_locked(
                    matter.matter_id, ACTOR, next_result["full_text_synthesis_input"]))
    finally:
        other.close()


@pytest.mark.parametrize("route", ["inspector", "export"])
def test_response_rechecks_membership_after_the_final_original_source_scan(
        workspace, monkeypatch, route):
    client, bench, matter, _ = workspace
    run, _, statements = seed_terminal_review(bench, matter, count=1)
    job = finish(bench, queue(bench, matter, run))
    original = bench._validate_full_text_synthesis_sources
    scans = []

    def scan_then_revoke(*args, **kwargs):
        receipt = original(*args, **kwargs)
        scans.append(1)
        if len(scans) == 2:
            # Revocation occurs during the final potentially lengthy source
            # revalidation, after the earlier post-render membership check.
            with fixture_write(bench.workspace):
                bench.workspace.connection.execute(
                    "UPDATE workbench_matter_membership SET state='revoked' WHERE matter_id=?",
                    (matter.matter_id,))
        return receipt

    monkeypatch.setattr(bench, "_validate_full_text_synthesis_sources", scan_then_revoke)
    path = (f"/matters/{matter.slug}/research?job={job.job_id}" if route == "inspector"
            else f"/matters/{matter.slug}/research/{job.job_id}/export?format=json")
    response = client.get(path)
    assert len(scans) == 2
    assert response.status_code in {404, 409}
    assert not any(statement in response.text for statement in statements)


def test_synthesis_routes_audit_only_identifiers_state_and_format_and_replay_one_job(workspace):
    from dataclasses import asdict

    client, bench, matter, _ = workspace
    run, documents, statements = seed_terminal_review(bench, matter, count=2)
    human_note = "SyntheticAuditHumanNoteCanary: compare this dispatch with the original gate record."
    decision = bench.workspace.review_decision(matter.matter_id, ACTOR, run.run_id, documents[0].document_id)
    bench.workspace.adjudicate_review_decision(matter.matter_id, ACTOR, run.run_id,
        documents[0].document_id, human_decision="uncertain", note=human_note,
        expected_updated_at=decision.updated_at)
    criterion = bench.workspace.review_criterion(matter.matter_id, run.criterion_id)
    version = bench.workspace.review_criterion_version(matter.matter_id, run.criterion_version_id)
    page = client.get(f"/matters/{matter.slug}/full-review?criterion={run.criterion_id}&run={run.run_id}")
    assert page.status_code == 200
    data = {field: re.search(rf'name="{field}" value="([^"]+)"', page.text).group(1)
            for field in ("csrf_token", "request_key", "expected_snapshot")}
    create_path = f"/matters/{matter.slug}/full-review/{run.run_id}/synthesize"
    first = client.post(create_path, data=data, follow_redirects=False)
    replay = client.post(create_path, data=data, follow_redirects=False)
    assert first.status_code == replay.status_code == 303
    assert first.headers["location"] == replay.headers["location"]
    jobs = bench.workspace.research_jobs(matter.matter_id, ACTOR)
    assert len(jobs) == 1
    job = jobs[0]
    assert job.result["full_text_synthesis_input"]["human_decisions"]["counts"] == {"uncertain": 1}

    cancel = client.post(f"/matters/{matter.slug}/research/{job.job_id}/cancel",
                        data={"csrf_token": data["csrf_token"]}, follow_redirects=False)
    assert cancel.status_code == 303
    assert bench.workspace.research_job(matter.matter_id, ACTOR, job.job_id).state == "cancelled"
    retry = client.post(f"/matters/{matter.slug}/research/{job.job_id}/retry",
                       data={"csrf_token": data["csrf_token"]}, follow_redirects=False)
    assert retry.status_code == 303
    assert bench.workspace.research_job(matter.matter_id, ACTOR, job.job_id).state == "queued"
    claimed = bench.workspace.claim_research_job("synthetic-audit-worker")
    assert claimed.job_id == job.job_id
    completed = finish(bench, claimed)
    exported = client.get(f"/matters/{matter.slug}/research/{job.job_id}/export?format=json")
    assert exported.status_code == 200
    assert all(statement in exported.text for statement in statements)

    expected_details = {
        "full_text.synthesis.create": [{"created": True}, {"created": False}],
        "research.cancel": [{"state": "cancelled"}],
        "research.retry": [{"count": 0, "state": "queued"}],
        "research.export": [{"format": "json"}],
    }
    audit = bench.workspace.audit_events(matter.matter_id)
    for action, details in expected_details.items():
        events = [event for event in audit if event.action == action]
        assert len(events) == len(details)
        assert sorted(json.dumps(event.details, sort_keys=True) for event in events) == sorted(
            json.dumps(value, sort_keys=True) for value in details)
        for event in events:
            assert event.actor_principal_id == ACTOR and event.matter_id == matter.matter_id
            assert event.object_type == "research_job" and event.object_id == completed.job_id
            assert event.outcome == "success"
            assert re.fullmatch(r"request-[0-9a-f]{32}", event.request_id)
    serialized = json.dumps([asdict(event) for event in audit])
    for content in (criterion.title, version.instructions, human_note, documents[0].display_name,
                    matter.display_name, *statements):
        assert content not in serialized
    assert len(bench.workspace.research_jobs(matter.matter_id, ACTOR)) == 1


def test_two_workspace_connections_atomically_replay_one_synthesis_admission(workspace):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    from case_intelligence.full_text_synthesis_repository import FullTextSynthesisRepository
    from case_intelligence.workspace_store import WorkspaceStore

    _, bench, matter, model = workspace
    run, _, _ = seed_terminal_review(bench, matter, count=2, selected_set=True)
    service = bench._full_text_synthesis_service(matter)
    with bench.source_store(matter).mutation_guard():
        prepared = service.prepare(matter.matter_id, ACTOR, run.run_id)
    receipt = prepared["full_text_synthesis_input"]
    plan = {"full_text_synthesis_version": 1, "synthesis_version": 1,
            "run_id": run.run_id, "input_digest": receipt["input_digest"]}
    request_key = "research-request-" + "d" * 32
    other = WorkspaceStore(bench.workspace.path)
    barrier = threading.Barrier(2)

    def submit(store):
        repository = FullTextSynthesisRepository(connection=store.connection,
            lock=store._lock, authorize=store.membership)
        barrier.wait(timeout=5)
        return store.queue_research_job(matter.matter_id, ACTOR,
            receipt["question"], receipt["title"], request_key, receipt["source_set_id"],
            initial_plan=deepcopy(plan), initial_result=deepcopy(prepared),
            validate_locked=lambda: repository.validate_locked(matter.matter_id, ACTOR, receipt))

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            pending = [pool.submit(submit, store) for store in (bench.workspace, other)]
            results = [future.result(timeout=10) for future in pending]
        assert results[0][0].job_id == results[1][0].job_id
        assert sorted(created for _, created in results) == [False, True]
        jobs = bench.workspace.research_jobs(matter.matter_id, ACTOR)
        assert len(jobs) == 1
        assert jobs[0].state == "queued" and jobs[0].attempts == 0
        assert jobs[0].result == prepared and jobs[0].plan == plan
        events = bench.workspace.research_events(matter.matter_id, ACTOR, jobs[0].job_id)
        assert len(events) == 1 and events[0].state == "queued"
        assert not model.calls
        assert not bench.workspace.connection.in_transaction and not other.connection.in_transaction
    finally:
        other.close()


@pytest.mark.parametrize("route", ["inspector", "export"])
def test_response_rechecks_source_set_after_actual_original_scan(workspace, monkeypatch, route):
    from case_intelligence.pilot_uploads import PilotDocument
    from case_intelligence.workspace_store import WorkspaceStore

    client, bench, matter, _ = workspace
    run, documents, statements = seed_terminal_review(bench, matter, count=1, selected_set=True)
    job = finish(bench, queue(bench, matter, run))
    other = WorkspaceStore(bench.workspace.path)
    original = PilotDocument.iter_parsed_units
    scans = []

    def scan_then_change_scope(document, **kwargs):
        scans.append(document.document_id)
        for unit in original(document, **kwargs):
            if len(scans) == 2:
                # A separate workspace connection can commit this edit while
                # the final response scan holds the source-file mutation guard.
                other.remove_source_organization(matter.matter_id, documents[0].document_id)
                assert not other.source_set_document_ids(matter.matter_id, run.source_set_id)
            yield unit

    monkeypatch.setattr(PilotDocument, "iter_parsed_units", scan_then_change_scope)
    try:
        path = (f"/matters/{matter.slug}/research?job={job.job_id}" if route == "inspector"
                else f"/matters/{matter.slug}/research/{job.job_id}/export?format=json")
        response = client.get(path)
        assert len(scans) == 2
        assert response.status_code == 409
        assert statements[0] not in response.text
    finally:
        other.close()


@pytest.mark.parametrize("units_per_source", [1, 21])
def test_maximum_unsealed_population_uses_one_aggregate_streaming_budget(
        workspace, monkeypatch, units_per_source):
    from dataclasses import asdict
    import hashlib
    from case_intelligence.pilot_uploads import PilotDocument, PilotUnit

    _, bench, matter, model = workspace
    earlier, documents, _ = seed_terminal_review(bench, matter, count=1)
    store = bench.source_store(matter)
    template = documents[0]
    # Persist 1,000 actual derived-unit files. The new run is cancelled before
    # any inventory seals; its zero ledger-unit counts cannot bound this scan.
    rows = [asdict(PilotUnit(number, "Synthetic unsealed original.",
        excerpt_digest=hashlib.sha256(b"Synthetic unsealed original.").hexdigest()))
        for number in range(1, units_per_source + 1)]
    template.units = deepcopy(rows)
    for number in range(1, 1000):
        document = replace(template, document_id=f"{number:032x}",
            display_name=f"synthetic-unsealed-{number}.txt", name_key=f"synthetic-unsealed-{number}.txt",
            units=deepcopy(rows), units_file="")
        store.documents[document.document_id] = document
        documents.append(document)
    store._save([document.document_id for document in documents])
    bench._sync_source_catalog(matter, documents)
    queued = bench.workspace.queue_review_run(matter.matter_id, ACTOR,
        earlier.criterion_version_id, run_kind="full", review_mode="full_text")
    run = bench.workspace.cancel_review_run(matter.matter_id, ACTOR, queued.run_id)
    prepared = bench._full_text_synthesis_service(matter).prepare(matter.matter_id, ACTOR, run.run_id)
    receipt = prepared["full_text_synthesis_input"]
    assert len(receipt["sources"]) == 1000
    assert all(not source["inventory_sealed"] and source["unit_count"] == 0 for source in receipt["sources"])
    scanned, yielded, reads = [], [], []
    original = PilotDocument.iter_parsed_units

    def observed(document, **kwargs):
        assert kwargs["max_record_chars"] == 120_065_536
        assert callable(kwargs["budget_check"])
        charge = kwargs["read_check"]
        def read(count):
            reads.append(count)
            charge(count)
        scanned.append(document.document_id)
        for unit in original(document, **{**kwargs, "read_check": read}):
            yielded.append(unit.number)
            yield unit

    def forbidden(*args, **kwargs):
        raise AssertionError("Population validation must use actual bounded streaming, not a whole-file loader")

    monkeypatch.setattr(PilotDocument, "iter_parsed_units", observed)
    monkeypatch.setattr(PilotDocument, "parsed_units", forbidden)
    if units_per_source == 1:
        with store.mutation_guard():
            assert bench._validate_full_text_synthesis_sources(matter, prepared) == receipt
        assert len(scanned) == len(set(scanned)) == 1000
        assert len(yielded) == 1000
    else:
        with pytest.raises(WorkspaceProblem, match="source-validation limit"):
            bench.queue_full_text_synthesis(matter, ACTOR, run.run_id, "research-request-" + "c" * 32)
        assert 900 < len(scanned) < 1000
        assert len(scanned) == len(set(scanned))
        assert len(yielded) == 20_001  # one over-limit unit, then no further read
        assert not bench.workspace.research_jobs(matter.matter_id, ACTOR)
    assert reads and 0 < max(reads) <= 65_536
    assert not model.calls


@pytest.mark.parametrize("limit", ["text", "serialized", "record", "deadline"])
def test_preparation_refuses_at_bounded_actual_original_reader(workspace, monkeypatch, limit):
    import case_intelligence.workbench as module
    from case_intelligence.pilot_uploads import PilotDocument

    _, bench, matter, model = workspace
    run, documents, statements = seed_terminal_review(bench, matter, count=1)
    document = documents[0]
    path = bench.source_store(matter).derived / document.units_file
    original = PilotDocument.iter_parsed_units
    reads, yielded = [], []
    clock = [0.0]
    if limit == "text":
        monkeypatch.setattr(module, "MAX_SYNTHESIS_SCAN_CHARS", len(statements[0]) - 1)
    elif limit == "serialized":
        path.write_text(" " * 200_000 + path.read_text())
        monkeypatch.setattr(module, "MAX_SYNTHESIS_SCAN_SERIALIZED_CHARS", 1024)
    elif limit == "record":
        monkeypatch.setattr(module, "MAX_SYNTHESIS_SCAN_RECORD_CHARS", 64)
    else:
        monkeypatch.setattr(module, "monotonic", lambda: clock[0])

    def observed(source, **kwargs):
        charge = kwargs["read_check"]
        def read(count):
            reads.append(count)
            if limit == "deadline":
                clock[0] = 5.0
            charge(count)
        for unit in original(source, **{**kwargs, "read_check": read}):
            yielded.append(unit.number)
            yield unit

    monkeypatch.setattr(PilotDocument, "iter_parsed_units", observed)
    with pytest.raises(WorkspaceProblem, match="source-validation limit"):
        bench.queue_full_text_synthesis(matter, ACTOR, run.run_id, "research-request-" + "f" * 32)
    assert reads and max(reads) <= 65_536
    assert len(yielded) == (1 if limit == "text" else 0)
    assert not bench.workspace.research_jobs(matter.matter_id, ACTOR)
    assert not model.calls
