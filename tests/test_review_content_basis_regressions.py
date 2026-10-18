from __future__ import annotations

from tests.fixture_write import fixture_write

import hashlib
import io
import sqlite3
from dataclasses import asdict

import pytest

import case_intelligence.workbench as workbench_module
from case_intelligence.generation import UnavailableGenerator, VerifiedReviewDecision
from case_intelligence.workbench import CaseIntelligenceWorkbench
from case_intelligence.workspace_store import WorkspaceProblem, WorkspaceStore


ACTOR = "development-basis-reviewer"


def _workbench(runtime_dir):
    bench = CaseIntelligenceWorkbench(
        runtime_dir,
        generator=UnavailableGenerator(),
    )
    bench.workspace.upsert_principal(
        "test",
        ACTOR,
        "Basis Reviewer",
        ACTOR,
        preferred_principal_id=ACTOR,
    )
    if bench.full_review is not None:
        bench.full_review.close()
        bench.full_review = None
    return bench


def _matter_with_source(bench, name: str, text: bytes):
    matter = bench.create_matter(name, "Synthetic content-basis fixture", ACTOR)
    document, _change = bench.source_store(matter).store_stream(
        "generated-record.txt",
        "text/plain",
        io.BytesIO(text),
    )
    assert document.state == "ready"
    return matter, document


def _criterion(bench, matter):
    _record, version = bench.workspace.create_review_criterion(
        matter.matter_id,
        ACTOR,
        title="Generated event criterion",
        instructions="Include the generated event record.",
    )
    return version


def _claimed_review(bench, matter):
    version = _criterion(bench, matter)
    queued = bench.workspace.queue_review_run(
        matter.matter_id,
        ACTOR,
        version.criterion_version_id,
        run_kind="full",
    )
    claimed = bench.workspace.claim_review_run("basis-review-worker")
    assert claimed is not None and claimed.run_id == queued.run_id
    decision = bench.workspace.next_review_decision(queued.run_id)
    assert decision is not None and decision.source_basis_digest
    return claimed, decision


def test_review_snapshot_refuses_empty_content_basis_atomically(tmp_path) -> None:
    bench = _workbench(tmp_path / "runtime")
    try:
        affected, affected_document = _matter_with_source(
            bench,
            "Generated transitional matter",
            b"Generated event EVT-4821 occurred at 08:42:17.",
        )
        unaffected, _unaffected_document = _matter_with_source(
            bench,
            "Generated complete matter",
            b"Generated event EVT-9102 occurred at 09:14:03.",
        )
        affected_version = _criterion(bench, affected)
        unaffected_version = _criterion(bench, unaffected)
        with fixture_write(bench.workspace):
            bench.workspace.connection.execute(
                "UPDATE workbench_source_catalog SET content_basis_digest='' "
                "WHERE matter_id=? AND document_id=?",
                (affected.matter_id, affected_document.document_id),
            )

        with pytest.raises(WorkspaceProblem, match="source-content tracking"):
            bench.workspace.queue_review_run(
                affected.matter_id,
                ACTOR,
                affected_version.criterion_version_id,
                run_kind="full",
            )

        assert bench.workspace.connection.execute(
            "SELECT COUNT(*) FROM workbench_review_run WHERE matter_id=?",
            (affected.matter_id,),
        ).fetchone()[0] == 0
        assert bench.workspace.connection.execute(
            "SELECT COUNT(*) FROM workbench_review_decision WHERE matter_id=?",
            (affected.matter_id,),
        ).fetchone()[0] == 0
        assert bench.workspace.connection.execute(
            "SELECT COUNT(*) FROM workbench_review_event"
        ).fetchone()[0] == 0

        unaffected_run = bench.workspace.queue_review_run(
            unaffected.matter_id,
            ACTOR,
            unaffected_version.criterion_version_id,
            run_kind="full",
        )
        assert unaffected_run.snapshot_count == 1
    finally:
        bench.close()


def test_restart_fails_active_snapshot_with_existing_empty_basis(tmp_path) -> None:
    runtime = tmp_path / "runtime"
    original = _workbench(runtime)
    matter, _document = _matter_with_source(
        original,
        "Generated intermediate-version matter",
        b"Generated event EVT-4821 occurred at 08:42:17.",
    )
    version = _criterion(original, matter)
    queued = original.workspace.queue_review_run(
        matter.matter_id,
        ACTOR,
        version.criterion_version_id,
        run_kind="full",
    )
    with fixture_write(original.workspace):
        original.workspace.connection.execute(
            "UPDATE workbench_review_decision SET source_basis_digest='' "
            "WHERE run_id=?",
            (queued.run_id,),
        )
    original.close()

    reopened = WorkspaceStore(runtime / "workbench.sqlite")
    try:
        recovered = reopened.review_run(
            matter.matter_id,
            ACTOR,
            queued.run_id,
        )
        assert recovered.state == "failed"
        assert "predates exact source-content tracking" in recovered.message
        event = reopened.connection.execute(
            "SELECT state,stage,message FROM workbench_review_event "
            "WHERE run_id=? ORDER BY ordinal DESC LIMIT 1",
            (queued.run_id,),
        ).fetchone()
        assert event is not None
        assert (event["state"], event["stage"]) == ("failed", "failed")
        assert event["message"] == recovered.message
    finally:
        reopened.close()


def test_processing_rejects_active_snapshot_with_empty_basis(
    tmp_path, monkeypatch
) -> None:
    bench = _workbench(tmp_path / "runtime")
    try:
        matter, _document = _matter_with_source(
            bench,
            "Generated runtime-processing matter",
            b"Generated event EVT-4821 occurred at 08:42:17.",
        )
        claimed, decision = _claimed_review(bench, matter)
        with fixture_write(bench.workspace):
            bench.workspace.connection.execute(
                "UPDATE workbench_review_decision SET source_basis_digest='' "
                "WHERE run_id=? AND document_id=?",
                (claimed.run_id, decision.document_id),
            )
        decision = bench.workspace.next_review_decision(claimed.run_id)
        assert decision is not None and decision.source_basis_digest == ""
        monkeypatch.setattr(
            bench.generator,
            "classify_source",
            lambda **_kwargs: VerifiedReviewDecision(
                "include",
                "Generated event EVT-4821 occurred at 08:42:17.",
                ("S1",),
                False,
                0,
            ),
        )

        result = bench._process_review_decision(claimed, decision, lambda: False)

        assert result.decision == "needs_attention"
        assert result.citations == ()
        assert "source changed" in result.rationale.casefold()
    finally:
        bench.close()


def test_recording_rejects_active_snapshot_with_empty_basis(tmp_path) -> None:
    bench = _workbench(tmp_path / "runtime")
    try:
        matter, _document = _matter_with_source(
            bench,
            "Generated runtime-recording matter",
            b"Generated event EVT-4821 occurred at 08:42:17.",
        )
        claimed, decision = _claimed_review(bench, matter)
        with fixture_write(bench.workspace):
            bench.workspace.connection.execute(
                "UPDATE workbench_review_decision SET source_basis_digest='' "
                "WHERE run_id=? AND document_id=?",
                (claimed.run_id, decision.document_id),
            )

        bench.workspace.record_review_decision(
            claimed.run_id,
            decision.document_id,
            decision="included",
            rationale="Generated supported inclusion.",
        )

        saved = bench.workspace.review_decision(
            matter.matter_id,
            ACTOR,
            claimed.run_id,
            decision.document_id,
        )
        assert saved.machine_decision == "needs_attention"
        assert saved.citations == ()
        assert "replacement was not reviewed" in saved.rationale
    finally:
        bench.close()


def test_finalization_rejects_saved_decision_with_empty_basis(tmp_path) -> None:
    bench = _workbench(tmp_path / "runtime")
    try:
        matter, _document = _matter_with_source(
            bench,
            "Generated runtime-finalization matter",
            b"Generated event EVT-4821 occurred at 08:42:17.",
        )
        claimed, decision = _claimed_review(bench, matter)
        bench.workspace.record_review_decision(
            claimed.run_id,
            decision.document_id,
            decision="included",
            rationale="Generated supported inclusion.",
        )
        with fixture_write(bench.workspace):
            bench.workspace.connection.execute(
                "UPDATE workbench_review_decision SET source_basis_digest='' "
                "WHERE run_id=? AND document_id=?",
                (claimed.run_id, decision.document_id),
            )

        finished = bench._finish_review_run(claimed)

        assert finished.state == "succeeded"
        assert finished.included_count == 0
        assert finished.attention_count == 1
        saved = bench.workspace.review_decision(
            matter.matter_id,
            ACTOR,
            claimed.run_id,
            decision.document_id,
        )
        assert saved.machine_decision == "needs_attention"
        assert saved.citations == ()
        assert "replacement was not reviewed" in saved.rationale
    finally:
        bench.close()


def test_finalization_revalidates_decisions_beyond_one_export_page(
    tmp_path, monkeypatch
) -> None:
    bench = _workbench(tmp_path / "runtime")
    try:
        matter = bench.create_matter(
            "Generated paged-finalization matter",
            "Synthetic content-basis fixture",
            ACTOR,
        )
        source_store = bench.source_store(matter)
        for index in range(1, 4):
            document, _change = source_store.store_stream(
                f"generated-record-{index}.txt",
                "text/plain",
                io.BytesIO(f"Generated source content {index}.".encode()),
            )
            assert document.state == "ready"
        claimed, _first = _claimed_review(bench, matter)
        decisions = bench.workspace.review_decisions_for_export(
            matter.matter_id, ACTOR, claimed.run_id
        )
        assert len(decisions) == 3
        for decision in decisions:
            bench.workspace.record_review_decision(
                claimed.run_id,
                decision.document_id,
                decision="included",
                rationale="Generated supported inclusion.",
            )
        with fixture_write(bench.workspace):
            for decision, ordinal in zip(decisions, (99_999, 100_000, 100_001)):
                bench.workspace.connection.execute(
                    "UPDATE workbench_review_decision SET ordinal=? "
                    "WHERE run_id=? AND document_id=?",
                    (ordinal, claimed.run_id, decision.document_id),
                )

        tail = source_store.get(decisions[-1].document_id)
        changed_text = "Generated replacement content beyond the first decision page."
        unit = asdict(tail.parsed_units()[0])
        unit.update(
            {
                "text": changed_text,
                "excerpt_digest": hashlib.sha256(changed_text.encode()).hexdigest(),
            }
        )
        tail.units = [unit]
        with source_store.mutation_guard():
            source_store._save((tail.document_id,))

        original_page = bench.workspace.review_decisions_for_export
        cursors: list[int] = []

        def two_at_a_time(
            matter_id: str,
            actor_id: str,
            run_id: str,
            *,
            limit: int = 100_000,
            after_ordinal: int = 0,
            administrator_override: bool = False,
        ):
            cursors.append(after_ordinal)
            kwargs = {
                "limit": min(limit, 2),
                "administrator_override": administrator_override,
            }
            if after_ordinal:
                kwargs["after_ordinal"] = after_ordinal
            return original_page(matter_id, actor_id, run_id, **kwargs)

        monkeypatch.setattr(
            bench.workspace, "review_decisions_for_export", two_at_a_time
        )
        finished = bench._finish_review_run(claimed)

        assert cursors == [0, 100_000, 100_001]
        assert finished.state == "succeeded"
        assert finished.included_count == 2
        assert finished.attention_count == 1
        saved = bench.workspace.review_decision(
            matter.matter_id,
            ACTOR,
            claimed.run_id,
            tail.document_id,
        )
        assert saved.machine_decision == "needs_attention"
        assert saved.citations == ()
        assert "replacement was not reviewed" in saved.rationale

        other = bench.create_matter(
            "Generated pagination isolation matter", "Synthetic", ACTOR
        )
        with pytest.raises(KeyError):
            original_page(
                other.matter_id,
                ACTOR,
                claimed.run_id,
                after_ordinal=100_000,
            )
    finally:
        bench.close()


class _PostgresCursor:
    def __init__(self, *, projection_failure: bool = False) -> None:
        self.projection_failure = projection_failure

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def execute(self, *_args, **_kwargs) -> None:
        return None

    def fetchall(self):
        if self.projection_failure:
            raise RuntimeError("synthetic PostgreSQL projection outage")
        return []


class _PostgresConnection:
    def __init__(
        self,
        *,
        projection_failure: bool = False,
        rollback_failure: bool = False,
    ) -> None:
        self.rollbacks = 0
        self.projection_failure = projection_failure
        self.rollback_failure = rollback_failure

    def cursor(self):
        return _PostgresCursor(projection_failure=self.projection_failure)

    def commit(self) -> None:
        return None

    def rollback(self) -> None:
        self.rollbacks += 1
        if self.rollback_failure:
            raise RuntimeError("synthetic closed PostgreSQL connection")

    def close(self) -> None:
        return None


@pytest.mark.parametrize("rollback_failure", (False, True))
def test_postgres_retention_failure_does_not_skip_legacy_basis_repair(
    tmp_path, monkeypatch, rollback_failure
) -> None:
    runtime = tmp_path / "runtime"
    original = _workbench(runtime)
    matter, document = _matter_with_source(
        original,
        "Generated migrated matter",
        b"Generated machine record EVT-4821 occurred at 08:42:17.",
    )
    expected_basis = original.workspace.source_catalog_record(
        matter.matter_id, document.document_id
    ).content_basis_digest
    assert len(expected_basis) == 64
    original.close()

    # fixture-write: Open migration fixture only after closing the original workbench and workers.
    database = sqlite3.connect(runtime / "workbench.sqlite")
    try:
        # fixture-write: Remove the basis column while the original workbench remains closed.
        database.execute(
            "ALTER TABLE workbench_source_catalog DROP COLUMN content_basis_digest"
        )
        database.commit()  # fixture-write: Commit the offline migration fixture while the original workbench is closed.
    finally:
        database.close()  # fixture-write: Close the offline migration connection while the original workbench and workers remain stopped.

    postgres = _PostgresConnection(rollback_failure=rollback_failure)
    monkeypatch.setattr(
        workbench_module,
        "apply_postgres_migrations",
        lambda _cursor: None,
    )

    def retention_unavailable(*_args, **_kwargs):
        raise RuntimeError("synthetic PostgreSQL retention outage")

    monkeypatch.setattr(
        workbench_module,
        "retain_postgres_workbench_matters",
        retention_unavailable,
    )

    reopened = CaseIntelligenceWorkbench(
        runtime,
        generator=UnavailableGenerator(),
        postgres_connection=postgres,
        learned_retrieval=True,
    )
    try:
        repaired = reopened.workspace.source_catalog_record(
            matter.matter_id, document.document_id
        )
        assert postgres.rollbacks == 1
        assert reopened.learned_retrieval is False
        assert repaired.content_basis_digest == expected_basis

        assert reopened.full_review is not None
        reopened.full_review.close()
        reopened.full_review = None
        version = _criterion(reopened, matter)
        queued = reopened.workspace.queue_review_run(
            matter.matter_id,
            ACTOR,
            version.criterion_version_id,
            run_kind="full",
        )
        claimed = reopened.workspace.claim_review_run("basis-review-worker")
        assert claimed is not None and claimed.run_id == queued.run_id
        decision = reopened.workspace.next_review_decision(queued.run_id)
        assert decision is not None
        assert decision.source_basis_digest == expected_basis

        source_store = reopened.source_store(matter)
        current = source_store.get(document.document_id)
        current_version = current.version_id
        edited = "Generated machine record EVT-4821 now states 09:03:11."
        unit = asdict(current.parsed_units()[0])
        current.units = [
            {
                **unit,
                "text": edited,
                "excerpt_digest": hashlib.sha256(edited.encode("utf-8")).hexdigest(),
            }
        ]
        source_store._save((current.document_id,))
        assert current.version_id == current_version

        result = reopened._process_review_decision(claimed, decision, lambda: False)
        assert result.decision == "needs_attention"
        assert result.citations == ()
        assert "source changed" in result.rationale.casefold()
    finally:
        reopened.close()


def test_postgres_projection_and_rollback_failure_keeps_repairing_catalogs(
    tmp_path, monkeypatch
) -> None:
    runtime = tmp_path / "runtime"
    original = _workbench(runtime)
    first_matter, first_document = _matter_with_source(
        original,
        "Generated first projection matter",
        b"Generated machine record EVT-4821 occurred at 08:42:17.",
    )
    second_matter, second_document = _matter_with_source(
        original,
        "Generated second projection matter",
        b"Generated machine record EVT-9102 occurred at 09:14:03.",
    )
    expected = {
        first_matter.matter_id: original.workspace.source_catalog_record(
            first_matter.matter_id, first_document.document_id
        ).content_basis_digest,
        second_matter.matter_id: original.workspace.source_catalog_record(
            second_matter.matter_id, second_document.document_id
        ).content_basis_digest,
    }
    original.close()

    # fixture-write: Open migration fixture only after closing the original workbench and workers.
    database = sqlite3.connect(runtime / "workbench.sqlite")
    try:
        # fixture-write: Remove the basis column while the original workbench remains closed.
        database.execute(
            "ALTER TABLE workbench_source_catalog DROP COLUMN content_basis_digest"
        )
        database.commit()  # fixture-write: Commit the offline migration fixture while the original workbench is closed.
    finally:
        database.close()  # fixture-write: Close the offline migration connection while the original workbench and workers remain stopped.

    postgres = _PostgresConnection(
        projection_failure=True,
        rollback_failure=True,
    )
    monkeypatch.setattr(
        workbench_module,
        "apply_postgres_migrations",
        lambda _cursor: None,
    )
    monkeypatch.setattr(
        workbench_module,
        "retain_postgres_workbench_matters",
        lambda *_args, **_kwargs: None,
    )

    reopened = CaseIntelligenceWorkbench(
        runtime,
        generator=UnavailableGenerator(),
        postgres_connection=postgres,
        learned_retrieval=True,
    )
    try:
        assert postgres.rollbacks == 1
        assert reopened.postgres_ready is False
        assert reopened.learned_retrieval is False
        assert reopened.workspace.source_catalog_record(
            first_matter.matter_id, first_document.document_id
        ).content_basis_digest == expected[first_matter.matter_id]
        assert reopened.workspace.source_catalog_record(
            second_matter.matter_id, second_document.document_id
        ).content_basis_digest == expected[second_matter.matter_id]
    finally:
        reopened.close()
