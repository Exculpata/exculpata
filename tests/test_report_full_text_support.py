"""Optional full-text Report adapter contracts, runnable without text-review storage."""
from types import SimpleNamespace

import pytest

from case_intelligence.report_materials import snapshot_report_materials
from case_intelligence.report_review_basis import review_sections
from case_intelligence import report_materials
from case_intelligence.pilot_uploads import PilotDocument
from case_intelligence.workspace_store import WorkspaceProblem
from tests.test_report_materials import ACTOR, MATTER, REVISION, answer, bench_for, message, reference, source
from tests.test_report_review_basis import workspace  # noqa: F401 - pytest fixture registration


def saved_check(bench, document, *, citations=None, human=False):
    canonical = {**reference(bench, document), "kind": "source"}
    compact = {key: value for key, value in canonical.items() if key != "excerpt"}
    compact.update(locator_kind="text_unit", unit_ordinal=1, unit_digest=canonical["excerpt_digest"])
    decision = SimpleNamespace(document_id=document.document_id, source_version_id=document.version_id,
        source_name=document.display_name, source_basis_digest=bench._document_content_basis(document),
        machine_decision="excluded" if human else "included", human_decision="include" if human else "",
        reviewed_by="", rationale="Saved synthetic screening.", human_note="Synthetic disagreement." if human else "",
        citations=(compact,) if citations is None else citations, error_message="", updated_at=REVISION,
        ordinal=1, validation_sample=1, reviewed_at=REVISION if human else None)
    run = SimpleNamespace(run_id="review-a", state="succeeded", snapshot_count=1, updated_at=REVISION,
                          criterion_version_id="criterion-version-synthetic")
    bench.workspace.runs[run.run_id] = run
    bench.workspace.decisions[run.run_id] = (decision,)
    return run, decision, canonical


def snapshot(bench, resolver, selections=("review:review-a",)):
    return snapshot_report_materials(bench, MATTER, ACTOR, selections, full_text_support_resolver=resolver)


def test_full_text_adapter_keeps_canonical_support_scope_and_disagreement_without_reloading(monkeypatch):
    document = source(1)
    bench, store = bench_for(document)
    run, decision, canonical = saved_check(bench, document, human=True)
    calls = []
    def resolve(matter, frozen_run, item):
        assert matter == MATTER and frozen_run == run and item == decision
        calls.append(item.document_id)
        return (canonical,)
    store.get = lambda _: pytest.fail("Verified source was materialized again")
    result = snapshot(bench, resolve)
    assert calls == [document.document_id]
    assert len(result[0].citations) == 1
    assert result[0].citations[0]["excerpt"] == canonical["excerpt"]
    assert result[0].citations[0]["kind"] == "source"
    assert result[0].origin == "human" and result[0].review_status == "disputed"
    assert "Synthetic disagreement" in result[0].text
    assert "complete reviewed ranges remain in the original ledger" in result[0].text
    assert decision.source_basis_digest in result[0].review_details
    assert "not the complete range ledger" in result[-1].text


@pytest.mark.parametrize("citations", [(), ({"locator_kind": "text_unit"},)])
def test_every_full_text_decision_including_uncited_override_must_pass_frozen_resolver(citations):
    document = source(1)
    bench, _ = bench_for(document)
    run, decision, _ = saved_check(bench, document, citations=citations, human=True)
    calls = []
    def stale(matter, frozen_run, item):
        calls.append((frozen_run.run_id, item.document_id, item.source_version_id, item.source_basis_digest))
        raise WorkspaceProblem("Synthetic frozen source changed.")
    with pytest.raises(WorkspaceProblem, match="frozen source changed"):
        snapshot(bench, stale)
    assert calls == [(run.run_id, decision.document_id, decision.source_version_id, decision.source_basis_digest)]


@pytest.mark.parametrize("case", ["oversize", "empty", "missing", "dropped", "too_many"])
def test_adapter_cannot_shorten_drop_or_overfill_support(case):
    document = source(1)
    bench, _ = bench_for(document)
    _, decision, canonical = saved_check(bench, document)
    values = [canonical]
    if case == "oversize":
        values = [{**canonical, "excerpt": "x" * 6001}]
    elif case == "empty":
        values = [{**canonical, "excerpt": " "}]
    elif case == "missing":
        values = [{key: value for key, value in canonical.items() if key != "excerpt"}]
    elif case == "dropped":
        values = []
    else:
        values *= 101
        decision.citations *= 101
    with pytest.raises(WorkspaceProblem):
        snapshot(bench, lambda *args: values)


def test_full_text_and_legacy_support_share_aggregate_budget(monkeypatch):
    document = source(1)
    bench, _ = bench_for(document)
    _, _, canonical = saved_check(bench, document)
    bench.workspace.chat = [message(1, answer(reference(bench, document)))]
    monkeypatch.setattr(report_materials, "MAX_TOTAL_CITATION_CHARS", 2 * len(canonical["excerpt"]) - 1)
    with pytest.raises(WorkspaceProblem, match="total citation-text limit"):
        snapshot(bench, lambda *args: (canonical,), ("review:review-a", "conversation:conversation-a"))


def test_compact_support_requires_explicit_adapter():
    document = source(1)
    bench, _ = bench_for(document)
    saved_check(bench, document)
    with pytest.raises(WorkspaceProblem, match="needs its source resolver"):
        snapshot(bench, None)


def test_saved_review_summary_uses_full_text_scope_and_preserves_full_citation():
    document = source(1, "x" * 6000)
    bench, _ = bench_for(document)
    run, decision, canonical = saved_check(bench, document)
    calls = []
    def resolve(item, values):
        calls.append((item, values))
        return (canonical,)
    sections = review_sections(run, (decision,), criterion_title="Synthetic criterion", criterion_version=1,
        instructions="Synthetic screening", ledger_path="/matters/synthetic/full-review", review_mode="full_text",
        citation_resolver=resolve)
    assert calls == [(decision, decision.citations)]
    assert "not the complete range ledger" in sections[0]["body"]
    assert "selected passages" not in sections[0]["body"]
    assert sections[3]["citations"][0]["excerpt"] == "x" * 6000
    with pytest.raises(WorkspaceProblem, match="6,000-character"):
        review_sections(run, (decision,), criterion_title="Synthetic criterion", criterion_version=1,
            instructions="Synthetic screening", ledger_path="/matters/synthetic/full-review", review_mode="full_text",
            citation_resolver=lambda *args: ({**canonical, "excerpt": "x" * 6001},))


@pytest.mark.parametrize("changed", [None, "source_version_id", "source_name", "location", "support_token", "excerpt", "kind"])
def test_final_report_validator_streams_and_keeps_every_exact_identity_check(monkeypatch, changed):
    document = source(1)
    bench, _ = bench_for(document)
    canonical = {**reference(bench, document), "kind": "source"}
    units = document.parsed_units()
    calls = []
    def stream(self):
        calls.append(self.document_id)
        yield from units
    monkeypatch.setattr(PilotDocument, "iter_parsed_units", stream, raising=False)
    monkeypatch.setattr(PilotDocument, "parsed_units", lambda _: pytest.fail("Materialized streaming document"))
    if changed:
        canonical[changed] = "Synthetic changed value"
        with pytest.raises(WorkspaceProblem, match="no longer resolves"):
            bench._assert_current_report_section_citations(MATTER, ({"citations": (canonical,)},))
    else:
        bench._assert_current_report_section_citations(MATTER, ({"citations": (canonical, canonical)},))
    assert calls == [document.document_id]


def test_full_text_workflow_payload_preserves_transcript_kind():
    document = source(1, transcript=True)
    bench, _ = bench_for(document)
    _, _, canonical = saved_check(bench, document)
    canonical.pop("kind")
    result = snapshot(bench, lambda *args: (canonical,))
    assert result[0].citations[0]["kind"] == "transcript"


def test_flow_calls_adapter_again_before_save_and_refuses_changed_frozen_source(workspace):
    from tests.test_report_review_basis import ACTOR as principal, saved_research
    from tests.test_guided_reports import queue, finished
    client, bench, matter = workspace
    bench.full_review.close()
    _, document = saved_research(bench, matter)
    _, version = bench.workspace.create_review_criterion(matter.matter_id, principal,
        title="Synthetic text check", instructions="Include bicycle records.")
    run = bench.workspace.queue_review_run(matter.matter_id, principal, version.criterion_version_id, run_kind="full")
    bench.workspace.claim_review_run("synthetic-text-report-worker")
    candidate = bench._candidate(matter, document, document.parsed_units()[0], 1)
    canonical = bench._workflow_citation_payload(bench._citation(matter, candidate))
    bench.workspace.record_review_decision(run.run_id, document.document_id, decision="included",
        rationale="Synthetic saved text screening.", citations=[canonical])
    bench.workspace.finish_review_run(run.run_id)
    calls = []
    def resolve(current_matter, saved_run, decision):
        assert current_matter.matter_id == matter.matter_id and saved_run.run_id == run.run_id
        assert decision.document_id == document.document_id
        calls.append(decision.source_basis_digest)
        if len(calls) == 2:
            raise WorkspaceProblem("The synthetic frozen source changed before save.")
        return (canonical,)
    bench.report_compilation.full_text_support_resolver = resolve
    job_id = queue(client, matter, [f"review:{run.run_id}"], key="synthetic-text-report-revalidation")
    outcome = finished(client, matter, job_id)
    assert outcome["state"] == "failed" and "changed before save" in outcome["message"]
    assert len(calls) == 2 and all(calls)
    assert not bench.workspace.reports(matter.matter_id, principal)


@pytest.mark.parametrize("tail", [
    None,
    "],\"version\":1",
    ", {\"number\":",
    "],\"version\":2}",
    "],\"version\":1} trailing content",
])
def test_direct_review_report_requires_valid_derived_container_tail(workspace, tail):
    import json
    from tests.test_report_review_basis import ACTOR as principal, saved_research
    client, bench, matter = workspace
    bench.full_review.close()
    _, document = saved_research(bench, matter)
    store = bench.source_store(matter)
    unit = document.parsed_units()[0]
    _, version = bench.workspace.create_review_criterion(matter.matter_id, principal,
        title="Synthetic trailer validation", instructions="Include bicycle records.")
    run = bench.workspace.queue_review_run(matter.matter_id, principal,
        version.criterion_version_id, run_kind="full")
    bench.workspace.claim_review_run("synthetic-trailer-worker")
    canonical = bench._workflow_citation_payload(bench._citation(
        matter, bench._candidate(matter, document, unit, 1)))
    bench.workspace.record_review_decision(run.run_id, document.document_id,
        decision="included", rationale="Synthetic saved check.", citations=[canonical])
    bench.workspace.finish_review_run(run.run_id)
    derived = store.derived / document.units_file
    record = json.loads(derived.read_text())["units"][0]
    # The first cited record is valid; only exhaustion can validate its trailer.
    derived.write_text('{"units":[' + json.dumps(record) + (tail or '],"version":1}'))
    response = client.post(f"/matters/{matter.slug}/full-review/{run.run_id}/report",
                           follow_redirects=False)
    assert response.status_code == 303
    reports = bench.workspace.reports(matter.matter_id, principal)
    if tail is None:
        assert "/reports?report=" in response.headers["location"] and len(reports) == 1
    else:
        assert "error=" in response.headers["location"]
        assert not reports


def test_final_report_validator_drains_without_matching_uncited_tail(monkeypatch):
    document = source(1)
    bench, _ = bench_for(document)
    canonical = {**reference(bench, document), "kind": "source"}
    unit = document.parsed_units()[0]
    completed = []
    def stream(self):
        yield unit
        for _ in range(200):
            yield unit
        completed.append(True)
    monkeypatch.setattr(PilotDocument, "iter_parsed_units", stream)
    original = bench._candidate
    matched = []
    def candidate(*args):
        matched.append(True)
        return original(*args)
    monkeypatch.setattr(bench, "_candidate", candidate)
    bench._assert_current_report_section_citations(MATTER, ({"citations": (canonical,)},))
    assert completed == [True] and len(matched) == 1
