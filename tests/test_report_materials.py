"""Synthetic saved-work adapter checks, without running a model or worker."""
from dataclasses import asdict
import hashlib
from types import SimpleNamespace

import pytest

from case_intelligence.pilot_uploads import PilotDocument, PilotUnit
from case_intelligence.report_materials import snapshot_report_materials
from case_intelligence import report_materials
from case_intelligence.work_product_exports import MAX_EXPORT_TEXT_CHARS
from case_intelligence.workbench import CaseIntelligenceWorkbench
from case_intelligence.workspace_store import NotebookReferenceRecord, WorkspaceProblem

MATTER = SimpleNamespace(matter_id="ci-matter-" + "a" * 32, slug="m-" + "a" * 12)
ACTOR = "synthetic-reviewer"
REVISION = "2026-01-02T03:04:05Z"


def source(index, text="The synthetic blue device arrived at North Annex.", *, transcript=False):
    unit = PilotUnit(1, text, line_start=1000 if transcript else None,
        line_end=2000 if transcript else None, excerpt_digest=hashlib.sha256(text.encode()).hexdigest())
    return PilotDocument(f"{index:032x}", f"Synthetic source {index}.{'wav' if transcript else 'txt'}",
        "", "audio/wav" if transcript else "text/plain", 0, "ready", "", [asdict(unit)],
        version_id=f"{index + 100:032x}")


class Store:
    def __init__(self, documents):
        self.documents = {item.document_id: item for item in documents}
        self.traversals = 0

    def get(self, document_id):
        return self.documents[document_id]

    def ready_documents(self):
        self.traversals += 1
        return tuple(item for item in self.documents.values() if item.state == "ready")


class Workspace:
    def __init__(self):
        self.chat = []
        self.notes = []
        self.refs = {}
        self.jobs = {}
        self.runs = {}
        self.decisions = {}
        self.streamed = 0

    def membership(self, matter_id, actor):
        if matter_id != MATTER.matter_id or actor != ACTOR:
            raise KeyError(actor)

    def get_conversation_any(self, matter_id, identifier):
        if matter_id != MATTER.matter_id or identifier != "conversation-a":
            raise KeyError(identifier)
        return SimpleNamespace(conversation_id=identifier, title="Synthetic conversation")

    def messages(self, matter_id, identifier):
        self.get_conversation_any(matter_id, identifier)
        return tuple(self.chat)

    def all_notebook_items(self, matter_id, actor, **kwargs):
        self.membership(matter_id, actor)
        return tuple(self.notes)[:kwargs["limit"]]

    def notebook_item(self, matter_id, actor, identifier):
        self.membership(matter_id, actor)
        return next(item for item in self.notes if item.item_id == identifier)

    def notebook_references(self, matter_id, actor, identifier):
        self.membership(matter_id, actor)
        return self.refs.get(identifier, ())

    def research_job(self, matter_id, actor, identifier):
        self.membership(matter_id, actor)
        return self.jobs[identifier]

    def review_run(self, matter_id, actor, identifier):
        self.membership(matter_id, actor)
        return self.runs[identifier]

    def iter_review_decisions_for_report(self, matter_id, actor, identifier):
        self.review_run(matter_id, actor, identifier)
        self.streamed += 1
        yield from self.decisions[identifier]

    def review_decisions_for_export(self, *args, **kwargs):
        pytest.fail("Adapter used the capped export API")

    def get_principal(self, identifier):
        return SimpleNamespace(display_name="Synthetic teammate")


def bench_for(*documents):
    bench = object.__new__(CaseIntelligenceWorkbench)
    bench.workspace = Workspace()
    def source_store(matter):
        return store

    bench.source_store = source_store
    bench.notebook_reference_from_support = lambda *args: pytest.fail("Per-reference matter scan")
    bench.available_notebook_support_tokens = lambda *args: pytest.fail("Per-note source scan")
    store = Store(documents)
    return bench, store


def reference(bench, document, *, staff=False):
    candidate = bench._candidate(MATTER, document, document.parsed_units()[0], 1)
    result = {
        "matter_id": MATTER.matter_id, "document_id": document.document_id,
        "source_version_id": document.version_id, "source_name": document.display_name,
        "location": candidate.citation, "support_token": bench._support_token(candidate),
        "unit_number": 1, "chunk_id": "chunk-1", "excerpt_digest": candidate.excerpt_digest,
        "excerpt": candidate.text, "line_start": candidate.line_start, "line_end": candidate.line_end,
        "evidence_kind": candidate.evidence_kind,
    }
    return {key: result[key] for key in ("source_name", "location", "support_token", "evidence_kind")} if staff else result


def message(index, payload=None, *, role="assistant", text="Saved synthetic response"):
    return SimpleNamespace(message_id=f"message-{index}", role=role, payload=payload or {}, content=text, created_at=REVISION)


def answer(ref, text="Saved synthetic finding"):
    return {"answerable": True, "introduction": "", "claims": [{"text": text, "citations": [ref]}],
        "limitation": None, "missing_information": ""}


def selected(bench, *selections):
    return snapshot_report_materials(bench, MATTER, ACTOR, selections or ("conversation:conversation-a",))


def test_unknown_staff_locators_use_one_matter_traversal_and_load_each_source_once():
    documents = [source(index) for index in range(1, 5)]
    bench, store = bench_for(*documents)
    refs = [reference(bench, item, staff=True) for item in documents]
    calls = {item.document_id: 0 for item in documents}
    for item in documents:
        units = item.parsed_units()
        def load(_, item=item, units=units, **kwargs):
            calls[item.document_id] += 1
            return units
        item.units = []
        item.units_file = "synthetic-derived.json"
        item._units_iterator = load
        item._units_loader = lambda _: pytest.fail("Materialized a file-backed source")
    bench.workspace.chat = [message(index, answer(refs[index % 4])) for index in range(80)]
    result = selected(bench)
    assert len(result) == 80
    assert store.traversals == 1
    assert set(calls.values()) == {1}


def test_known_locators_avoid_scanning_unselected_documents_and_keep_long_full_text():
    document = source(1, "Synthetic full passage " + "x" * 5900)
    unrelated = source(2)
    bench, store = bench_for(document, unrelated)
    unrelated._units_loader = lambda _: pytest.fail("Unselected document loaded")
    unrelated.units, unrelated.units_file = [], "synthetic-unselected.json"
    bench.workspace.chat = [message(1, answer(reference(bench, document)))]
    result = selected(bench)
    assert result[0].citations[0]["excerpt"] == document.parsed_units()[0].text
    assert len(result[0].citations[0]["excerpt"]) <= 6000
    assert store.traversals == 0


def test_oversized_canonical_unit_fails_instead_of_truncating():
    document = source(1, "x" * 6_001)
    bench, _ = bench_for(document)
    bench.workspace.chat = [message(1, answer(reference(bench, document)))]
    with pytest.raises(WorkspaceProblem, match="6,000-character"):
        selected(bench)


def test_repeated_full_citations_fit_exact_aggregate_boundary():
    document = source(1, "x" * 5_000)
    bench, _ = bench_for(document)
    ref = reference(bench, document)
    assert report_materials.MAX_TOTAL_CITATION_CHARS == MAX_EXPORT_TEXT_CHARS // 2
    count = report_materials.MAX_TOTAL_CITATION_CHARS // len(ref["excerpt"])
    payload = answer(ref)
    payload["claims"][0]["citations"] = [ref] * count
    bench.workspace.chat = [message(1, payload)]
    result = selected(bench)
    citations = result[0].citations
    assert len(citations) == count
    assert sum(len(value["excerpt"]) for value in citations) == report_materials.MAX_TOTAL_CITATION_CHARS
    assert all(value["excerpt"] == ref["excerpt"] for value in citations)


@pytest.mark.parametrize("across_findings", [False, True])
def test_duplicate_citations_are_charged_per_rendered_occurrence(monkeypatch, across_findings):
    document = source(1, "x" * 5_000)
    bench, _ = bench_for(document)
    ref = reference(bench, document)
    monkeypatch.setattr(report_materials, "MAX_TOTAL_CITATION_CHARS", 9_999)
    if across_findings:
        bench.workspace.chat = [message(index, answer(ref)) for index in (1, 2)]
    else:
        payload = answer(ref)
        payload["claims"][0]["citations"] = [ref, ref]
        bench.workspace.chat = [message(1, payload)]
    with pytest.raises(WorkspaceProblem, match="total citation-text limit"):
        selected(bench)


def test_cumulative_canonical_budget_stops_before_loading_later_sources(monkeypatch):
    documents = [source(1, "a" * 4_000), source(2, "b" * 4_000),
                 source(3, "c" * 2_001), source(4)]
    bench, _ = bench_for(*documents)
    refs = [reference(bench, document) for document in documents]
    # A saved preview can be short while the canonical passage is long.
    for ref in refs:
        ref["excerpt"] = ref["excerpt"][:20]
    bench.workspace.chat = [message(index, answer(ref)) for index, ref in enumerate(refs)]
    monkeypatch.setattr(report_materials, "MAX_TOTAL_CITATION_CHARS", 10_000)
    documents[-1].units, documents[-1].units_file = [], "synthetic-unneeded.json"
    documents[-1]._units_loader = lambda _: pytest.fail("Loaded a source after citation budget exhaustion")
    with pytest.raises(WorkspaceProblem, match="total citation-text limit"):
        selected(bench)


def test_citation_budget_is_shared_across_selected_saved_work(monkeypatch):
    document = source(1, "x" * 5_000)
    bench, _ = bench_for(document)
    ref = reference(bench, document)
    bench.workspace.chat = [message(1, answer(ref))]
    bench.workspace.notes = [SimpleNamespace(item_id="note-a", title="Synthetic note", body="Saved finding",
        status="needs_review", updated_at=REVISION, updated_by_name="Synthetic reviewer",
        created_by_name="Synthetic reviewer", date_label="", item_type="note")]
    fields = NotebookReferenceRecord.__dataclass_fields__
    values = {key: value for key, value in ref.items() if key in fields}
    values.update(reference_id="reference-a", item_id="note-a", ordinal=1, created_at=REVISION)
    bench.workspace.refs["note-a"] = (NotebookReferenceRecord(**values),)
    monkeypatch.setattr(report_materials, "MAX_TOTAL_CITATION_CHARS", 9_999)
    with pytest.raises(WorkspaceProblem, match="total citation-text limit"):
        selected(bench, "conversation:conversation-a", "note:note-a")


def test_unversioned_transcript_staff_reference_fails_even_though_token_resolves():
    document = source(1, transcript=True)
    bench, _ = bench_for(document)
    bench.workspace.chat = [message(1, answer(reference(bench, document, staff=True)))]
    with pytest.raises(WorkspaceProblem, match="exact saved text version"):
        selected(bench)


def test_frozen_transcript_reference_cannot_silently_substitute_an_edit():
    document = source(1, transcript=True)
    bench, _ = bench_for(document)
    frozen = reference(bench, document)
    bench.workspace.chat = [message(1, answer(frozen))]
    assert selected(bench)[0].citations[0]["excerpt"] == frozen["excerpt"]
    replacement = source(1, "The synthetic blue device did not arrive.", transcript=True)
    document.units = replacement.units
    assert reference(bench, document)["support_token"] == frozen["support_token"]
    with pytest.raises(WorkspaceProblem, match="source support changed"):
        selected(bench)


def test_digest_bound_legacy_transcript_reference_remains_exact():
    document = source(1, transcript=True)
    bench, _ = bench_for(document)
    value = reference(bench, document, staff=True)
    candidate = bench._candidate(MATTER, document, document.parsed_units()[0], 1)
    value["support_token"] = bench._legacy_support_token(candidate)
    bench.workspace.chat = [message(1, answer(value))]
    assert selected(bench)[0].citations[0]["excerpt"] == candidate.text


@pytest.mark.parametrize("field,value", [("matter_id", "ci-matter-" + "b" * 32),
    ("document_id", "f" * 32), ("source_version_id", "f" * 32), ("excerpt", "altered text")])
def test_foreign_and_stale_locator_fields_fail_closed(field, value):
    document = source(1)
    bench, _ = bench_for(document)
    ref = reference(bench, document)
    ref[field] = value
    bench.workspace.chat = [message(1, answer(ref))]
    with pytest.raises(WorkspaceProblem):
        selected(bench)


def test_membership_is_checked_before_any_source_lookup():
    bench, store = bench_for(source(1))
    with pytest.raises(KeyError):
        snapshot_report_materials(bench, MATTER, "synthetic-outsider", ("conversation:conversation-a",))
    assert store.traversals == 0


def test_cited_qualification_coverage_and_user_correction_survive():
    document = source(1)
    bench, _ = bench_for(document)
    ref = reference(bench, document)
    payload = answer(ref)
    payload.update(limitation={"text": "The source does not identify who delivered it.", "citations": [ref]},
        evidence_notice="One potentially relevant source is not ready.",
        review_scope={"notice": "Selected passages only.", "candidate_source_count": 1},
        source_coverage={"notice": "One source was searched.", "searchable": 1})
    bench.workspace.chat = [message(1, role="user", text="Correction: I meant the north annex, not the south site."), message(2, payload)]
    result = selected(bench)
    human = next(item for item in result if item.origin == "human")
    assert not human.citations and human.review_status == "needs_review"
    assert "Correction:" in human.text and human.author == "Conversation participant"
    limitation = next(item for item in result if item.material_id.endswith(":limitation"))
    assert limitation.category == "gap" and limitation.citations
    assert any(item.category == "coverage" and "candidate_source_count" in item.review_details for item in result)
    assert any("not ready" in item.text for item in result)


def test_abstention_without_missing_information_keeps_its_saved_explanation():
    bench, _ = bench_for()
    bench.workspace.chat = [message(1, {"answerable": False, "claims": [], "introduction": "The sources do not establish the date."})]
    result = selected(bench)
    assert result[0].category == "gap" and "do not establish" in result[0].text


def test_notebook_prefix_with_full_digest_becomes_full_canonical_support(monkeypatch):
    document = source(1, "Synthetic note source " + "y" * 5900)
    bench, _ = bench_for(document)
    value = reference(bench, document)
    ref_fields = {key: val for key, val in value.items() if key in NotebookReferenceRecord.__dataclass_fields__}
    ref = NotebookReferenceRecord(**ref_fields, reference_id="ref-1", item_id="note-1", ordinal=1, created_at=REVISION)
    ref = NotebookReferenceRecord(**{**asdict(ref), "excerpt": ref.excerpt[:200]})
    note = SimpleNamespace(item_id="note-1", status="confirmed", title="Synthetic note", body="Saved human correction", updated_at=REVISION,
        updated_by_name="Synthetic teammate", created_by_name="Synthetic teammate", date_label="", item_type="note")
    bench.workspace.notes = [note]
    bench.workspace.refs = {note.item_id: (ref,)}
    monkeypatch.setattr("case_intelligence.report_materials.MAX_MATERIALS", 1)
    result = selected(bench, "notes:active", "note:note-1")
    assert len(result) == 1 and result[0].citations[0]["excerpt"] == value["excerpt"]


def test_research_claims_resolve_against_the_frozen_evidence_ledger():
    document = source(1, transcript=True)
    bench, _ = bench_for(document)
    frozen = reference(bench, document)
    payload = answer(reference(bench, document, staff=True))
    job = SimpleNamespace(job_id="research-a", matter_id=MATTER.matter_id, state="succeeded", title="Synthetic investigation", updated_at=REVISION, plan={},
        result={"evidence": [frozen], "answer": payload, "summary": "Saved synthetic finding", "passes": [], "gaps": [],
                "coverage": {"notice": "Selected passages only."}})
    bench.workspace.jobs[job.job_id] = job
    assert selected(bench, "research:research-a")[0].citations[0]["excerpt"] == frozen["excerpt"]
    document.units = source(1, "A revised synthetic transcript.", transcript=True).units
    with pytest.raises(WorkspaceProblem):
        selected(bench, "research:research-a")


def test_machine_only_source_checks_are_streamed_preserved_and_counted():
    document = source(1)
    bench, _ = bench_for(document)
    decision = SimpleNamespace(document_id=document.document_id, source_version_id=document.version_id,
        source_name=document.display_name, source_basis_digest=bench._document_content_basis(document),
        machine_decision="included", human_decision="", reviewed_by="", rationale="Saved synthetic screening rationale.",
        human_note="", citations=(reference(bench, document),), error_message="", updated_at=REVISION)
    bench.workspace.runs["review-a"] = SimpleNamespace(run_id="review-a", state="succeeded", snapshot_count=1, updated_at=REVISION)
    bench.workspace.decisions["review-a"] = (decision,)
    result = selected(bench, "review:review-a")
    assert bench.workspace.streamed == 1
    machine = next(item for item in result if item.category != "coverage")
    assert machine.origin == "source_review_ai" and "not been reviewed" in machine.text
    assert machine.citations
    assert any("Awaiting human review: 1" in item.text for item in result)
    document.units = source(1, "A changed synthetic source.").units
    with pytest.raises(WorkspaceProblem):
        selected(bench, "review:review-a")


def test_uncited_human_override_still_validates_frozen_source_content():
    document = source(1)
    bench, _ = bench_for(document)
    decision = SimpleNamespace(document_id=document.document_id, source_version_id=document.version_id,
        source_name=document.display_name, source_basis_digest=bench._document_content_basis(document),
        machine_decision="excluded", human_decision="include", reviewed_by="", rationale="Synthetic screening.",
        human_note="Synthetic human override without a passage citation.", citations=(), error_message="", updated_at=REVISION)
    bench.workspace.runs["review-a"] = SimpleNamespace(run_id="review-a", state="succeeded", snapshot_count=1, updated_at=REVISION)
    bench.workspace.decisions["review-a"] = (decision,)
    result = selected(bench, "review:review-a")
    assert result[0].origin == "human" and result[0].review_status == "disputed" and not result[0].citations
    document.units = source(1, "A changed synthetic source after the override.").units
    with pytest.raises(WorkspaceProblem):
        selected(bench, "review:review-a")


@pytest.mark.parametrize("state", ["processing", "failed", "cancelled"])
def test_uncited_review_cannot_use_unavailable_source_with_same_content(state):
    document = source(1)
    bench, _ = bench_for(document)
    decision = SimpleNamespace(document_id=document.document_id, source_version_id=document.version_id,
        source_name=document.display_name, source_basis_digest=bench._document_content_basis(document),
        machine_decision="excluded", human_decision="include", reviewed_by="", rationale="Synthetic screening.",
        human_note="Synthetic uncited override.", citations=(), error_message="", updated_at=REVISION)
    bench.workspace.runs["review-a"] = SimpleNamespace(run_id="review-a", state="succeeded", snapshot_count=1, updated_at=REVISION)
    bench.workspace.decisions["review-a"] = (decision,)
    document.state = state
    with pytest.raises(WorkspaceProblem):
        selected(bench, "review:review-a")


@pytest.mark.parametrize('origin', ['conversation', 'research'])
@pytest.mark.parametrize('legacy', [False, True])
@pytest.mark.parametrize('sourced_limitation', [False, True])
def test_saved_answer_round_trip_keeps_service_notice_uncited(tmp_path, monkeypatch, origin, legacy, sourced_limitation):
    import sys
    from case_intelligence.generation import EvidenceItem, GroundedGenerationService, VERIFICATION_OMISSION_NOTICE
    from case_intelligence.workspace_store import WorkspaceStore

    workspace = WorkspaceStore(tmp_path / 'synthetic-provenance.sqlite')
    try:
        workspace.upsert_principal('test', ACTOR, 'Synthetic Reviewer', ACTOR, preferred_principal_id=ACTOR)
        matter = workspace.create_matter('Synthetic saved-answer provenance', '', ACTOR)
        monkeypatch.setattr(sys.modules[__name__], 'MATTER', matter)
        documents = (source(1, 'A synthetic delivery was recorded.'),
                     source(2, 'The synthetic delivery date remains unconfirmed.'))
        bench, _ = bench_for(*documents)
        bench.workspace = workspace
        class PartlySupportedClient:
            available = True
            def generate(self, *, evidence, **kwargs):
                return {'answerable': True, 'claims': [
                    {'text': evidence[0].excerpt, 'evidence_ids': ['S1']},
                    {'text': 'An unsupported helicopter arrived at 99:99.', 'evidence_ids': ['S1']},
                ], 'limitation': {'text': evidence[1].excerpt, 'evidence_ids': ['S2']} if sourced_limitation else None,
                    'missing_information': ''}
        service = GroundedGenerationService(PartlySupportedClient())
        citations = {f'S{index}': bench._citation(matter, bench._candidate(matter, document, document.parsed_units()[0], 1))
                     for index, document in enumerate(documents, 1)}
        evidence = tuple(EvidenceItem(key, value.source_name, value.location, value.excerpt)
                         for key, value in citations.items())
        verified = service.answer('What is recorded about the synthetic delivery?', evidence)
        assert verified.omitted_claims == 1
        payload = bench._answer_payload(verified, citations)
        assert payload['verification_notice'] == VERIFICATION_OMISSION_NOTICE
        assert (payload['source_limitation'] is not None) == sourced_limitation
        if legacy:
            payload.pop('source_limitation')
            payload.pop('verification_notice')
        if origin == 'conversation':
            conversation = workspace.get_conversation(matter.matter_id)
            workspace.append_message(matter.matter_id, conversation.conversation_id, 'assistant', verified.text, payload)
            saved = workspace.messages(matter.matter_id, conversation.conversation_id)[-1].payload
            selections = (f'conversation:{conversation.conversation_id}',)
        else:
            queued, _ = workspace.queue_research_job(matter.matter_id, ACTOR, 'Synthetic delivery?', 'Synthetic investigation',
                                                     'research-request-' + 'a'*32)
            workspace.claim_research_job('synthetic-provenance-worker')
            result = {'evidence': [bench._workflow_citation_payload(value) for value in citations.values()],
                      'answer': payload, 'summary': verified.text, 'passes': [], 'gaps': [],
                      'coverage': {'notice': 'Selected passages only.'}}
            workspace.finish_research_job(queued.job_id, result)
            saved = workspace.research_job(matter.matter_id, ACTOR, queued.job_id).result['answer']
            selections = (f'research:{queued.job_id}',)
        assert saved == payload
        materials = snapshot_report_materials(bench, matter, ACTOR, selections)
        notices = [item for item in materials if VERIFICATION_OMISSION_NOTICE in item.text]
        assert len(notices) == 1 and not notices[0].citations
        assert notices[0].category == 'coverage'
        qualifications = [item for item in materials if item.material_id.endswith(':limitation')]
        assert len(qualifications) == int(sourced_limitation)
        if sourced_limitation:
            assert qualifications[0].text == documents[1].parsed_units()[0].text
            assert len(qualifications[0].citations) == 1
            assert VERIFICATION_OMISSION_NOTICE not in qualifications[0].text
    finally:
        workspace.close()
