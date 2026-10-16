"""An expired synthetic matter survives scheduler and job passes.

Deletion still requires a fresh confirmation after the seven-day window.
"""

from datetime import datetime, timedelta, timezone
import threading
import time

import pytest

from case_intelligence.generation import UnavailableGenerator
from case_intelligence.workbench import CaseIntelligenceWorkbench, MatterMaintenanceCoordinator
from case_intelligence.workspace_store import WorkspaceProblem
from tests.case_lifecycle_helpers import lifecycle_session


OWNER = "principal-owner"
NAME = "Synthetic expired matter"
FIXED = datetime(2026, 3, 1, tzinfo=timezone.utc)


def test_expired_matter_survives_scheduler_and_jobs_until_explicit_confirmation(tmp_path):
    now = [FIXED]
    bench = CaseIntelligenceWorkbench(
        tmp_path / "runtime",
        generator=UnavailableGenerator(),
        learned_retrieval=False,
        answer_workers=1,
    )
    try:
        bench.workspace._clock = lambda: now[0]
        bench.workspace.upsert_principal(
            "test", "owner", "Generated owner", "owner", preferred_principal_id=OWNER,
        )
        matter = bench.create_matter(NAME, "Generated fixture", OWNER, retention_days=1)
        expires_at = bench.workspace._timestamp(FIXED - timedelta(days=40))
        purge_after = bench.workspace._timestamp(FIXED - timedelta(days=30))
        with bench.workspace._lock:
            bench.workspace.connection.execute(
                "INSERT INTO workbench_matter_retention VALUES (?,?,?,?,?,?)",
                (matter.matter_id, expires_at, purge_after, OWNER, expires_at, expires_at),
            )
            bench.workspace.connection.commit()
        conversation = bench.workspace.get_conversation(matter.matter_id)
        job, created = bench.workspace.queue_answer_job(
            matter.matter_id,
            conversation.conversation_id,
            OWNER,
            "What does the synthetic record say?",
            "answer-request-" + "a" * 32,
        )
        assert created is True
        bench.answers.notify()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            current_job = bench.workspace.get_answer_job(matter.matter_id, OWNER, job.job_id)
            if current_job.state in {"failed", "succeeded", "cancelled"}:
                break
            time.sleep(0.02)
        else:
            current_job = bench.workspace.get_answer_job(matter.matter_id, OWNER, job.job_id)
        assert current_job.state == "failed"

        now[0] = FIXED + timedelta(days=400)
        policy = bench.workspace.case_lifecycle.policy(matter.matter_id)
        assert policy.legacy_purge_after == purge_after
        assert bench.retention_projection(matter)["status"] == "ownership_review"
        assert bench.workspace.due_matter_retentions() == ()
        assert bench.workspace.begin_due_matter_purge(matter.matter_id, source_count=0) is None

        completed = threading.Event()
        outcome = {}

        def maintenance_pass():
            try:
                outcome.update(bench.run_maintenance_once())
                return outcome
            finally:
                completed.set()

        bench.maintenance = MatterMaintenanceCoordinator(maintenance_pass, interval_seconds=900)
        assert completed.wait(10), "The maintenance scheduler did not finish a pass."
        bench.maintenance.close()
        bench.maintenance = None
        assert outcome == {"uploads_cleaned": 0, "matters_purged": 0, "matters_deferred": 0}
        assert bench.workspace.matter_lifecycle(matter.matter_id).state == "active"
        assert bench.workspace.get_matter_by_id(matter.matter_id).display_name == NAME
        assert bench.workspace.matter_retention(matter.matter_id).purge_after == purge_after

        session = lifecycle_session(bench.workspace, OWNER)
        exit_record = bench.workspace.case_lifecycle.begin_exit(
            matter.slug, OWNER, mode="delete_without_export", session_id=session.session_id,
        )
        eligible = datetime.fromisoformat(exit_record.eligible_after.replace("Z", "+00:00"))
        assert eligible - now[0] >= timedelta(days=7)
        now[0] = eligible - timedelta(microseconds=1)
        with pytest.raises(WorkspaceProblem, match="seven-day"):
            bench.workspace.case_lifecycle.issue_confirmation(
                matter.slug, OWNER, session.session_id, mode="delete_without_export",
            )
        assert bench.run_maintenance_once()["matters_purged"] == 0
        assert bench.workspace.matter_lifecycle(matter.matter_id).state == "closing"

        now[0] = eligible
        assert bench.run_maintenance_once()["matters_purged"] == 0
        assert bench.workspace.case_lifecycle.read_matter(matter.slug, OWNER).display_name == NAME
        assert bench.workspace.matter_lifecycle(matter.matter_id).state == "closing"
        with pytest.raises(WorkspaceProblem, match="fresh deletion confirmation"):
            bench.workspace.begin_matter_purge(
                matter.slug,
                OWNER,
                NAME,
                source_count=0,
                token="synthetic-rejected-confirmation",
                session_id=session.session_id,
                mode="delete_without_export",
                no_backup_acknowledged=True,
            )
        assert bench.workspace.matter_lifecycle(matter.matter_id).state == "closing"
        nonce = bench.workspace.case_lifecycle.issue_confirmation(
            matter.slug, OWNER, session.session_id, mode="delete_without_export",
        )
        _matter, lifecycle = bench.workspace.case_lifecycle.claim_purge(
            matter.slug,
            OWNER,
            NAME,
            token=nonce.token,
            session_id=session.session_id,
            mode="delete_without_export",
            no_backup_acknowledged=True,
            source_count=0,
        )
        assert lifecycle.state == "purging"
        assert lifecycle.purge_id
    finally:
        bench.close()
