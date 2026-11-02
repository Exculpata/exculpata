"""Synthetic bounded source validation and reviewed-decision Report regressions."""
from dataclasses import asdict, replace
import hashlib
import json
from types import SimpleNamespace
import weakref
from concurrent.futures import ThreadPoolExecutor

import pytest

from case_intelligence import report_materials
from case_intelligence import report_compilation_flow, unit_stream
from case_intelligence.generation import GroundedGenerationService
from case_intelligence.pilot_uploads import PilotDocument, PilotStore, PilotUnit
from case_intelligence.report_compilation import compile_report
from case_intelligence.workspace_store import WorkspaceProblem
from tests.test_report_materials import (
    REVISION, answer, bench_for, message, reference, selected, source,
)
from tests.test_guided_reports import queue, finished
from tests.test_report_compilation import SourceEchoClient
from tests.test_report_review_basis import ACTOR as PRINCIPAL, saved_research, workspace  # noqa: F401 - pytest fixture registration


def save_decisions(bench, documents, *, machine="included", human="agree", citations=True):
    decisions = tuple(SimpleNamespace(
        document_id=document.document_id, source_version_id=document.version_id,
        source_name=document.display_name, source_basis_digest=bench._document_content_basis(document),
        machine_decision=machine, human_decision=human, reviewed_by="synthetic-reviewer",
        rationale="Synthetic saved screening.", human_note="Synthetic saved review.",
        citations=(reference(bench, document),) if citations else (), error_message="", updated_at=REVISION,
    ) for document in documents)
    bench.workspace.runs["review-a"] = SimpleNamespace(
        run_id="review-a", state="succeeded", snapshot_count=len(decisions), updated_at=REVISION)
    bench.workspace.decisions["review-a"] = decisions
    return decisions


@pytest.mark.parametrize("machine,human,status", [
    ("included", "agree", "confirmed"), ("excluded", "agree", "confirmed"),
    ("included", "include", "confirmed"), ("excluded", "exclude", "confirmed"),
    ("included", "exclude", "disputed"), ("excluded", "include", "disputed"),
    ("included", "uncertain", "needs_review"), ("excluded", "uncertain", "needs_review"),
    ("included", "", "needs_review"), ("needs_review", "agree", "needs_review"),
    ("failed", "agree", "needs_review"), ("uncertain", "agree", "needs_review"),
    ("needs_review", "include", "needs_review"),
])
def test_saved_human_agreement_retains_resolved_status_in_report(machine, human, status):
    document = source(1)
    bench, _ = bench_for(document)
    save_decisions(bench, (document,), machine=machine, human=human)
    material = selected(bench, "review:review-a")[0]
    assert material.review_status == status
    if human:
        draft = compile_report("timeline", materials=(material,))
        heading = draft.sections[0]["heading"]
        assert ("Human review" in heading) == (status == "confirmed")
        assert ("Disagreements and open review" in heading) == (status != "confirmed")
        assert "Synthetic saved review." in draft.sections[0]["body"]
        assert draft.sections[0]["citations"] == material.citations


def file_back(documents, tmp_path, *, tracked=None):
    store = PilotStore(tmp_path / "sources", malware_scan_mode="disabled")
    for document in documents:
        store._write_units(document)
        def stream(name, *, _document=document, **kwargs):
            if tracked is not None:
                tracked["calls"].append(_document.document_id)
            for unit in store._iter_units(name, **kwargs):
                if tracked is not None:
                    tracked["alive"] += 1
                    tracked["peak"] = max(tracked["peak"], tracked["alive"])
                    def released():
                        tracked["alive"] -= 1
                    weakref.finalize(unit, released)
                yield unit
        document._units_iterator = stream
        document._units_loader = lambda _: pytest.fail("Report materialized a whole derived source")
    return store


def large_source(index, *, count=80, width=10_000):
    document = source(index)
    for number in range(2, count + 1):
        text = (f"Synthetic uncited unit {number}. " + "x" * width)
        document.units.append(asdict(PilotUnit(number, text, line_start=number,
            line_end=number + 1, start_ms=number * 10, end_ms=number * 10 + 5,
            excerpt_digest=hashlib.sha256(text.encode()).hexdigest())))
    return document


def test_large_file_backed_sources_hash_once_and_retain_only_matched_units(tmp_path, monkeypatch):
    documents = [large_source(index) for index in (1, 2, 3)]
    bench, _ = bench_for(*documents)
    decisions = save_decisions(bench, documents)
    # A second selection refers to the same sources without document IDs.
    bench.workspace.chat = [message(index, answer(reference(bench, document, staff=True)))
                            for index, document in enumerate(documents)]
    tracked = {"calls": [], "alive": 0, "peak": 0}
    file_back(documents, tmp_path, tracked=tracked)
    monkeypatch.setattr(PilotDocument, "parsed_units", lambda _: pytest.fail("Report loaded every unit"))
    materials = selected(bench, "review:review-a", "conversation:conversation-a")
    assert len(materials) == 7
    assert tracked["calls"] == [document.document_id for document in documents]
    assert tracked["peak"] <= len(documents) + 2
    assert tracked["alive"] == 0
    assert all(len(item.citations) == 1 for item in materials if item.category != "coverage")
    assert all(item.source_basis_digest for item in decisions)


@pytest.mark.parametrize("limit", ["characters", "units", "serialized"])
def test_aggregate_scan_limit_counts_uncited_units_across_sources(tmp_path, monkeypatch, limit):
    documents = [large_source(index, count=4, width=2_000) for index in (1, 2, 3)]
    bench, _ = bench_for(*documents)
    bench.workspace.chat = [message(index, answer(reference(bench, document)))
                            for index, document in enumerate(documents)]
    tracked = {"calls": [], "alive": 0, "peak": 0}
    file_back(documents, tmp_path, tracked=tracked)
    if limit == "characters":
        monkeypatch.setattr(report_materials, "MAX_SOURCE_SCAN_CHARS", 8_000)
    elif limit == "units":
        monkeypatch.setattr(report_materials, "MAX_SOURCE_SCAN_UNITS", 5)
    else:
        monkeypatch.setattr(report_materials, "MAX_SOURCE_SCAN_SERIALIZED_CHARS", 10_000)
    with pytest.raises(WorkspaceProblem, match="source-validation limit"):
        selected(bench)
    assert tracked["calls"] == [document.document_id for document in documents[:2]]


@pytest.mark.parametrize("suffix", [" trailing", ",\"unexpected\":1}", "wrong-version"])
def test_first_citation_cannot_hide_invalid_derived_container(tmp_path, suffix):
    document = source(1)
    bench, _ = bench_for(document)
    bench.workspace.chat = [message(1, answer(reference(bench, document)))]
    store = file_back((document,), tmp_path)
    path = store.derived / document.units_file
    raw = path.read_text()
    if suffix == "wrong-version":
        raw = raw.replace('"version":1', '"version":2')
    elif suffix.startswith(","):
        raw = raw[:-1] + suffix
    else:
        raw += suffix
    path.write_text(raw)
    with pytest.raises(WorkspaceProblem, match="source support changed"):
        selected(bench)


def test_streamed_basis_hash_detects_uncited_text_with_stale_digest(tmp_path):
    document = large_source(1, count=4, width=100)
    bench, _ = bench_for(document)
    save_decisions(bench, (document,), citations=False)
    document.units[-1]["text"] = "Synthetic changed uncited text with unchanged digest."
    file_back((document,), tmp_path)
    with pytest.raises(WorkspaceProblem, match="source support changed"):
        selected(bench, "review:review-a")


@pytest.mark.parametrize("field", ["matter_id", "document_id", "source_version_id", "source_name",
                                   "location", "unit_number", "chunk_id", "excerpt_digest",
                                   "excerpt", "support_token", "kind"])
def test_final_snapshot_rejects_every_changed_citation_field(field):
    document = source(1)
    bench, _ = bench_for(document)
    bench.workspace.chat = [message(1, answer(reference(bench, document)))]
    materials = selected(bench)
    citation = materials[0].citations[0]
    report_materials.validate_compiled_material_citations(materials, ({"citations": (citation,)},))
    with pytest.raises(WorkspaceProblem, match="outside the current validated selection"):
        report_materials.validate_compiled_material_citations(materials,
            ({"citations": ({**citation, field: "synthetic changed value"},)},))


def test_report_record_limit_rejects_huge_first_unit_before_json_decode(tmp_path, monkeypatch):
    document = source(1, "x" * 5_000)
    bench, _ = bench_for(document)
    bench.workspace.chat = [message(1, answer(reference(bench, document)))]
    file_back((document,), tmp_path)
    monkeypatch.setattr(report_materials, "MAX_SOURCE_SCAN_RECORD_CHARS", 1_000)
    class CheckedDecoder(json.JSONDecoder):
        def raw_decode(self, value, *args, **kwargs):
            assert not value.startswith("{") or len(value) <= 1_000, "Oversized unit reached JSON decoding"
            return super().raw_decode(value, *args, **kwargs)
    monkeypatch.setattr(unit_stream.json, "JSONDecoder", CheckedDecoder)
    with pytest.raises(WorkspaceProblem, match="source-validation limit"):
        selected(bench)


def test_cooperative_snapshot_deadline_is_checked_after_a_file_read(tmp_path, monkeypatch):
    document = source(1)
    bench, _ = bench_for(document)
    bench.workspace.chat = [message(1, answer(reference(bench, document)))]
    file_back((document,), tmp_path)
    clock = {"now": 0.0}
    monkeypatch.setattr(report_materials, "time", SimpleNamespace(monotonic=lambda: clock["now"]))
    original = document._units_iterator
    def stream(name, **kwargs):
        read_check = kwargs["read_check"]
        def delayed_read(count):
            clock["now"] = report_materials.MAX_SOURCE_SCAN_SECONDS
            read_check(count)
        yield from original(name, **{**kwargs, "read_check": delayed_read})
    document._units_iterator = stream
    with pytest.raises(WorkspaceProblem, match="source-validation limit"):
        selected(bench)


def test_budgeted_report_refuses_legacy_materializing_file_loader():
    document = source(1)
    bench, _ = bench_for(document)
    bench.workspace.chat = [message(1, answer(reference(bench, document)))]
    document.units, document.units_file = [], "synthetic-derived.json"
    document._units_loader = lambda _: pytest.fail("Legacy materializing loader was called")
    with pytest.raises(WorkspaceProblem):
        selected(bench)


@pytest.mark.parametrize("phase", ["before-generation", "before-save"])
def test_snapshot_budget_failure_saves_nothing_and_releases_both_guards(workspace, monkeypatch, phase):
    client, bench, matter = workspace
    research, _ = saved_research(bench, matter)
    class BudgetClient(SourceEchoClient):
        def generate(self, **kwargs):
            result = super().generate(**kwargs)
            if phase == "before-save":
                monkeypatch.setattr(report_materials, "MAX_SOURCE_SCAN_CHARS", 0)
            return result
    bench.generator = GroundedGenerationService(BudgetClient())
    if phase == "before-generation":
        monkeypatch.setattr(report_materials, "MAX_SOURCE_SCAN_CHARS", 0)
    job_id = queue(client, matter, [f"research:{research.job_id}"])
    result = finished(client, matter, job_id)
    assert result["state"] == "failed" and "source-validation limit" in result["message"]
    assert not bench.workspace.reports(matter.matter_id, PRINCIPAL)
    def other_reviewer():
        with bench.source_store(matter).mutation_guard(), bench.workspace._lock:
            return bench.workspace.create_notebook_item(matter.matter_id, PRINCIPAL,
                item_type="note", status="confirmed", title="Synthetic follow-up",
                body="Reviewer can continue after the bounded validation failure.")
    with ThreadPoolExecutor(max_workers=1) as executor:
        assert executor.submit(other_reviewer).result(timeout=2)[0].title == "Synthetic follow-up"


def test_finish_rejects_forged_draft_citation_without_partial_report(workspace, monkeypatch):
    client, bench, matter = workspace
    research, _ = saved_research(bench, matter)
    bench.generator = GroundedGenerationService(SourceEchoClient())
    original = report_compilation_flow.compile_report
    def forged(*args, **kwargs):
        draft = original(*args, **kwargs)
        sections = list(draft.sections)
        index = next(index for index, section in enumerate(sections) if section["citations"])
        section = sections[index]
        citations = list(section["citations"])
        citations[0] = {**citations[0], "excerpt_digest": "f" * 64}
        sections[index] = {**section, "citations": tuple(citations)}
        return replace(draft, sections=tuple(sections))
    monkeypatch.setattr(report_compilation_flow, "compile_report", forged)
    monkeypatch.setattr(bench, "_assert_current_report_section_citations",
                        lambda *args: pytest.fail("Final save reread source files"))
    job_id = queue(client, matter, [f"research:{research.job_id}"])
    result = finished(client, matter, job_id)
    assert result["state"] == "failed" and "outside the current validated selection" in result["message"]
    assert not bench.workspace.reports(matter.matter_id, PRINCIPAL)
