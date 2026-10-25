"""Synthetic acceptance for cited, neutral witness inconsistency candidates."""

import hashlib
import re
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, nullcontext

import pytest

from case_intelligence.assertion_repository import AssertionRepository
from case_intelligence.assertion_service import AssertionService
from case_intelligence.entity_service import EntityService, REFERENCE_FIELDS
from case_intelligence.witness_inconsistency import WitnessInconsistencyService, _differences, _PLACE
from case_intelligence.workspace_store import WorkspaceProblem, WorkspaceStore
from tests.fixture_write import fixture_write
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


# Round 2 regressions use only generated accounts and references.
def test_same_witness_different_events_are_not_compared(tmp_path):
    store, matter, assertions, refs = setup_case(tmp_path)
    try:
        finder = WitnessInconsistencyService(assertions.repository, assertions.entities)
        original = finder.find(matter.matter_id, ACTOR)
        assert {row.comparison for row in original} == {
            'same witness', 'different witnesses on the same event'}
        account = store.connection.execute(
            "SELECT * FROM workbench_assertion_account WHERE support_token=?", ('b' * 40,)).fetchone()
        role = assertions.repository.roles(matter.matter_id, account['assertion_id'])[0]
        assertions.remove_account(matter.matter_id, ACTOR, account['assertion_id'],
            expected_revision=4, account_id=account['account_id'])
        assertions.create(matter.matter_id, ACTOR, title='Separate synthetic event',
            statement='A separate event.', record_type='event', support='b' * 40,
            attributed_to='Avery Example', roles=[dict(entity_id=role['entity_id'],
                expected_revision=1, role='witness')])
        refs.pop('c' * 40)
        refs.pop('d' * 40)
        assert finder.find(matter.matter_id, ACTOR) == ()
    finally:
        store.close()


def test_place_excludes_temporal_and_quantity_phrases():
    for phrase in ('at the time', 'at noon', 'at least three'):
        assert _PLACE.findall(phrase) == []
        assert _differences(phrase, 'at the gate') == ''


def test_place_stops_before_later_preposition():
    assert _PLACE.findall('at the gate beside the north wall') == ['gate', 'north wall']
    assert _differences('at the gate beside the north wall',
                        'at the gate beside the south wall') == 'place'


@pytest.mark.parametrize(('left', 'right'), [
    ('at the end of the pier', 'at the boat ramp'),
    ('at the night market', 'at the bus depot'),
])
def test_real_place_starting_with_temporal_word(left, right):
    assert _differences(left, right) == 'place'
    assert _differences(right, left) == 'place'


@pytest.mark.parametrize('phrase', ['at the end', 'at night'])
@pytest.mark.parametrize('suffix', ['', '.', ',', ';'])
def test_bare_end_and_night_idioms_are_not_places(phrase, suffix):
    assert _differences(phrase.upper() + suffix, 'at the gate') == ''


def test_one_sided_place_superset_is_not_a_difference():
    shorter = 'at the gate'
    longer = 'at the gate beside the north wall'
    assert _differences(shorter, longer) == ''
    assert _differences(longer, shorter) == ''


def test_time_with_meridiem_against_bare_hour_is_not_a_difference():
    assert _differences('5 pm', '7') == ''
    assert _differences('7', '5 pm') == ''


@pytest.mark.parametrize(('left', 'right', 'expected'), [
    ('9:00 am', '9:00 pm', 'time'),
    ('9:00am', '9:00pm', 'time'),
    ('9:00 pm', '21:00', ''),
    ('09:15', '9:15', ''),
])
def test_times_compare_normalized_minutes(left, right, expected):
    assert _differences(left, right) == expected
    assert _differences(left + ' with 3 parcels', right + ' with 3 parcels') == expected
    assert _differences('12:00 AM with 3 parcels', '00:00 with 3 parcels') == ''
    assert _differences('12:00 pm with 3 parcels', '12:00 with 4 parcels') == 'quantity'


def test_reattribution_reopens_dismissed_candidate(tmp_path):
    store, matter, assertions, _ = setup_case(tmp_path)
    try:
        finder = WitnessInconsistencyService(assertions.repository, assertions.entities)
        candidate = finder.find(matter.matter_id, ACTOR)[0]
        finder.dismiss(matter.matter_id, ACTOR, candidate.candidate_id)
        account = store.connection.execute('SELECT * FROM workbench_assertion_account WHERE account_id=?',
            (candidate.first.account_id,)).fetchone()
        assertions.revise_account(matter.matter_id, ACTOR, account['assertion_id'], expected_revision=4,
            account_id=account['account_id'], stance=account['stance'], attributed_to='Drew Synthetic')
        pair = {candidate.first.account_id, candidate.second.account_id}
        revised = [row for row in finder.find(matter.matter_id, ACTOR)
                   if {row.first.account_id, row.second.account_id} == pair]
        assert len(revised) == 1
        assert revised[0].candidate_id != candidate.candidate_id
        assert revised[0].review_state == 'unreviewed'
    finally:
        store.close()


@pytest.mark.parametrize('changed', ['excerpt', 'support_token', 'attributed_to', 'difference'])
def test_candidate_identity_includes_passage_content_and_difference(changed):
    first = dict(reference(0), account_id='account-a', assertion_id='event-a', attributed_to='Avery Example')
    second = dict(reference(1), account_id='account-b', assertion_id='event-a', attributed_to='Avery Example')
    create = WitnessInconsistencyService._candidate
    original = create('matter-a', first, second, 'same witness', 'time')
    assert create('matter-a', second, first, 'same witness', 'time').candidate_id == original.candidate_id
    difference = 'place' if changed == 'difference' else 'time'
    for passage in (first, second):
        edited = dict(passage)
        if changed != 'difference':
            edited[changed] += ' synthetic correction'
        left, right = (edited, second) if passage is first else (first, edited)
        assert create('matter-a', left, right, 'same witness', difference).candidate_id != original.candidate_id


@pytest.mark.parametrize('invalid', ['made-up', 'stale-source', 'other-matter', 'consistent'])
def test_review_rejects_noncurrent_candidate_without_writes(tmp_path, invalid):
    store, matter, assertions, refs = setup_case(tmp_path)
    try:
        finder = WitnessInconsistencyService(assertions.repository, assertions.entities)
        candidate = finder.find(matter.matter_id, ACTOR)[0]
        candidate_id, matter_id = candidate.candidate_id, matter.matter_id
        if invalid == 'made-up':
            candidate_id = 'witness-inconsistency-' + '0' * 64
        elif invalid == 'stale-source':
            refs.pop(candidate.first.support_token)
        elif invalid == 'other-matter':
            matter_id = store.create_matter('Other synthetic matter', '', ACTOR).matter_id
        else:
            refs[candidate.first.support_token]['excerpt'] = candidate.second.excerpt
            # fixture-write: simulate a changed derived passage with current synthetic support.
            with fixture_write(store) as connection:
                connection.execute('UPDATE workbench_assertion_account SET excerpt=? WHERE account_id=?',
                    (candidate.second.excerpt, candidate.first.account_id))
        before = [tuple(row) for row in store.connection.execute(
            'SELECT * FROM workbench_witness_inconsistency_review')]
        for update in (finder.mark_reviewed, finder.dismiss):
            with pytest.raises(WorkspaceProblem, match='current inconsistency candidate'):
                update(matter_id, ACTOR, candidate_id)
            assert [tuple(row) for row in store.connection.execute(
                'SELECT * FROM workbench_witness_inconsistency_review')] == before
    finally:
        store.close()


def test_legacy_dismissal_does_not_hide_current_candidate(tmp_path):
    store, matter, assertions, _ = setup_case(tmp_path)
    try:
        finder = WitnessInconsistencyService(assertions.repository, assertions.entities)
        candidates = finder.find(matter.matter_id, ACTOR)
        candidate = candidates[0]
        old_id = 'witness-inconsistency-' + hashlib.sha256((matter.matter_id + '\0' + '\0'.join(
            sorted((candidate.first.account_id, candidate.second.account_id)))).encode()).hexdigest()
        now = store._now()
        # fixture-write: retain a dismissal created by the previous account-only key format.
        with fixture_write(store) as connection:
            connection.execute('INSERT INTO workbench_witness_inconsistency_review VALUES (?,?,?,?,?)',
                (matter.matter_id, old_id, 'dismissed', ACTOR, now))
        assert finder.find(matter.matter_id, ACTOR) == candidates
    finally:
        store.close()


def test_thousand_account_scan_allows_writer_reservation(tmp_path, monkeypatch):
    store, matter, assertions, _ = setup_case(tmp_path)
    try:
        sample = dict(store.connection.execute(
            'SELECT * FROM workbench_assertion_account WHERE support_token=?', ('a' * 40,)).fetchone())
        # fixture-write: bulk seed 996 generated accounts for a 1,000-account read scan.
        with fixture_write(store) as connection:
            for index in range(996):
                row = dict(sample, account_id=f'synthetic-account-{index}',
                           attributed_to=f'Synthetic witness {index}')
                connection.execute('INSERT INTO workbench_assertion_account (' + ','.join(row) +
                    ') VALUES (' + ','.join('?' for _ in row) + ')', tuple(row.values()))
        active = []
        @contextmanager
        def guard():
            active.append(True)
            try:
                yield
            finally:
                active.pop()
        validate = assertions.entities.validate_references
        def reserve_writer():
            with sqlite3.connect(tmp_path / 'workspace.sqlite', timeout=0.2) as writer:
                # fixture-write: probe an independent writer reservation without the workspace lock.
                writer.execute('BEGIN IMMEDIATE')
                writer.rollback()
        def validate_during_scan(rows):
            assert active and store.connection.in_transaction
            assert len(rows) == 1000
            with ThreadPoolExecutor(max_workers=1) as pool:
                pool.submit(reserve_writer).result()
            return validate(rows)
        monkeypatch.setattr(assertions.entities, 'source_guard', guard)
        monkeypatch.setattr(assertions.entities, 'validate_references', validate_during_scan)
        finder = WitnessInconsistencyService(assertions.repository, assertions.entities)
        started = time.monotonic()
        results = finder.find(matter.matter_id, ACTOR)
        # A generous 60-second CI bound covers all 499,500 possible account pairs.
        assert time.monotonic() - started < 60
        assert results and not active
        assert all(row.label == 'possible inconsistency for review' for row in results)
        with pytest.raises(KeyError):
            finder.find(matter.matter_id, 'synthetic-outsider')
        assert not active
    finally:
        store.close()


@pytest.mark.parametrize(('left', 'right', 'expected'), [
    ('at 9:00 p.m.', 'at 21:00', ''),
    ('at 9:00 p.m.', 'at 9:00 a.m.', 'time'),
    ('at 9 pm', 'at 9 am', 'time'),
    ('at 21:00', 'at 9:00 pm', ''),
    ('at noon', 'at midnight', 'time'),
    ('arrived at midnight.', 'arrived at the gate.', ''),
    ('left at once.', 'left at the gate.', ''),
    ('seen at the end.', 'seen at the door.', ''),
    ('arrived at night.', 'arrived at the gate.', ''),
])
def test_residual_time_and_place_forms(left, right, expected):
    for suffix in (' beside the north gate.', ' by the gate.'):
        assert _differences(left + suffix, right + suffix) == expected
