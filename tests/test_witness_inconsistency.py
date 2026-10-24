"""Synthetic acceptance for cited, neutral witness inconsistency candidates."""

import re
from contextlib import nullcontext

from case_intelligence.assertion_repository import AssertionRepository
from case_intelligence.assertion_service import AssertionService
from case_intelligence.entity_service import EntityService, REFERENCE_FIELDS
from case_intelligence.witness_inconsistency import WitnessInconsistencyService
from case_intelligence.workspace_store import WorkspaceStore
from tests.case_lifecycle_helpers import claim_synthetic_exit

ACTOR = "synthetic-witness-reviewer"
PASSAGES = (
    "Avery Example stated the handoff occurred at 09:15 beside the north gate.",
    "Avery Example stated the handoff occurred at 10:45 beside the north gate.",
    "Blair Sample stated the handoff occurred at 09:15 beside the south gate.",
    "Casey Fixture also recalled 09:15 beside the north gate.",
)


def reference(index):
    letter = "abcd"[index]
    return dict(document_id="document-" + letter * 32, source_version_id="version-" + letter * 32,
        source_name=f"Synthetic statement {letter}.txt", location="Lines 1-1", unit_number=1,
        chunk_id="chunk-1", excerpt_digest=letter * 64, excerpt=PASSAGES[index], support_token=letter * 40)


def setup_case(tmp_path):
    store = WorkspaceStore(tmp_path / "workspace.sqlite")
    store.upsert_principal("test", ACTOR, "Synthetic reviewer", ACTOR, preferred_principal_id=ACTOR)
    matter = store.create_matter("Synthetic witness comparison", "", ACTOR)
    refs = {row["support_token"]: row for row in map(reference, range(4))}
    entities = EntityService(store.entity_repository(), source_guard=nullcontext, resolve_support=lambda token: refs[token],
        load_note=store.notebook_item, load_references=store.notebook_references,
        validate_references=lambda values: frozenset(i for i, value in enumerate(values)
            if value["support_token"] in refs and all(refs[value["support_token"]][key] == value[key]
                for key in REFERENCE_FIELDS)))
    assertions = AssertionService(AssertionRepository(store.entity_repository()), entities)
    witness = entities.create(matter.matter_id, ACTOR, display_name="Avery Example")
    event = assertions.create(matter.matter_id, ACTOR, title="Synthetic handoff", statement="Accounts of a handoff.",
        record_type="event", support="a" * 40, attributed_to="Avery Example",
        roles=[dict(entity_id=witness["entity_id"], expected_revision=1, role="witness")])
    event = assertions.attach(matter.matter_id, ACTOR, event["assertion_id"], expected_revision=1,
        support="b" * 40, attributed_to="Avery Example")
    event = assertions.attach(matter.matter_id, ACTOR, event["assertion_id"], expected_revision=2,
        support="c" * 40, attributed_to="Blair Sample")
    event = assertions.attach(matter.matter_id, ACTOR, event["assertion_id"], expected_revision=3,
        support="d" * 40, attributed_to="Casey Fixture")
    return store, matter, assertions, refs


def test_finder_reports_known_hits_without_consistent_false_positive(tmp_path):
    store, matter, assertions, refs = setup_case(tmp_path)
    finder = WitnessInconsistencyService(assertions.repository, assertions.entities)
    results = finder.find(matter.matter_id, ACTOR)

    pairs = {frozenset((row.first.excerpt, row.second.excerpt)) for row in results}
    time_pair = frozenset((PASSAGES[0], PASSAGES[1]))
    place_pair = frozenset((PASSAGES[0], PASSAGES[2]))
    consistent_pair = frozenset((PASSAGES[0], PASSAGES[3]))
    hits = sum(pair in pairs for pair in (time_pair, place_pair))
    false_positives = int(consistent_pair in pairs)
    print(f"witness inconsistency hits={hits}/2 false_positives={false_positives}/1")

    assert hits == 2
    assert false_positives == 0
    assert all(row.label == "possible inconsistency for review" for row in results)
    assert all(row.first.source_name and row.first.location and row.second.source_name and row.second.location
               for row in results)
    store.close()


def test_invalid_citations_are_dropped_and_result_language_is_neutral(tmp_path):
    store, matter, assertions, refs = setup_case(tmp_path)
    finder = WitnessInconsistencyService(assertions.repository, assertions.entities)
    refs.pop("b" * 40)
    results = finder.find(matter.matter_id, ACTOR)
    rendered = " ".join(f"{row.label} {row.note} {row.first.excerpt} {row.second.excerpt}" for row in results)

    assert PASSAGES[1] not in rendered
    assert not re.search(r"\b(lied|false|fabricated)\b", rendered, re.IGNORECASE)
    store.close()


def test_review_state_is_matter_scoped_persisted_and_removed_by_purge(tmp_path):
    store, matter, assertions, _ = setup_case(tmp_path)
    finder = WitnessInconsistencyService(assertions.repository, assertions.entities)
    candidates = finder.find(matter.matter_id, ACTOR)
    first, second = candidates[:2]
    finder.mark_reviewed(matter.matter_id, ACTOR, first.candidate_id)
    finder.dismiss(matter.matter_id, ACTOR, second.candidate_id)

    restarted = WitnessInconsistencyService(assertions.repository, assertions.entities)
    states = {row.candidate_id: row.review_state for row in restarted.find(matter.matter_id, ACTOR)}
    assert states[first.candidate_id] == "reviewed"
    assert second.candidate_id not in states

    _, lifecycle = claim_synthetic_exit(store, matter, ACTOR)
    store.complete_matter_purge(matter.matter_id, lifecycle.purge_id)
    assert store.connection.execute("SELECT COUNT(*) FROM workbench_witness_inconsistency_review").fetchone()[0] == 0
    store.close()
