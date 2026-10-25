"""Synthetic review transitions, bounded scans and citation guards."""

import json
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from threading import Event

import pytest

from case_intelligence import witness_inconsistency as witness_module
from tests.fixture_write import fixture_write

from case_intelligence.witness_inconsistency import WitnessInconsistencyService
from case_intelligence.workspace_store import WorkspaceProblem
from tests.test_witness_inconsistency import ACTOR, setup_case


@pytest.fixture
def review_case(tmp_path):
    store, matter, assertions, refs = setup_case(tmp_path)
    finder = WitnessInconsistencyService(assertions.repository, assertions.entities)
    try:
        yield store, matter.matter_id, finder, refs
    finally:
        store.close()


def review_rows(store):
    return [tuple(row) for row in store.connection.execute(
        'SELECT * FROM workbench_witness_inconsistency_review')]


def audit_rows(store):
    return [dict(row) for row in store.connection.execute(
        "SELECT * FROM workbench_audit_event WHERE action='witness_inconsistency.review' ORDER BY rowid")]


def test_dismissal_can_be_reversed(review_case):
    store, matter, finder, _ = review_case
    candidate = finder.find(matter, ACTOR)[0]
    finder.dismiss(matter, ACTOR, candidate.candidate_id)
    finder.mark_reviewed(matter, ACTOR, candidate.candidate_id)
    assert next(row for row in finder.find(matter, ACTOR)
                if row.candidate_id == candidate.candidate_id).review_state == 'reviewed'


def test_dismissed_list_and_reopen(review_case):
    store, matter, finder, _ = review_case
    candidate = finder.find(matter, ACTOR)[0]
    finder.dismiss(matter, ACTOR, candidate.candidate_id)
    assert candidate.candidate_id not in {row.candidate_id for row in finder.find(matter, ACTOR)}
    dismissed, = finder.list_dismissed(matter, ACTOR)
    assert dismissed.first == candidate.first and dismissed.second == candidate.second
    assert dismissed.review_state == 'dismissed'
    finder.reopen(matter, ACTOR, candidate.candidate_id)
    assert candidate in finder.find(matter, ACTOR)
    assert finder.list_dismissed(matter, ACTOR) == ()


def test_state_changes_are_audited_and_invalid_ids_write_nothing(review_case):
    store, matter, finder, _ = review_case
    candidate = finder.find(matter, ACTOR)[0]
    old_state = 'unreviewed'
    for method, state in [('dismiss', 'dismissed'), ('mark_reviewed', 'reviewed'), ('reopen', 'unreviewed')]:
        before = len(audit_rows(store))
        getattr(finder, method)(matter, ACTOR, candidate.candidate_id)
        rows = audit_rows(store)
        assert len(rows) == before + 1
        assert rows[-1]['matter_id'] == matter and rows[-1]['actor_principal_id'] == ACTOR
        assert rows[-1]['object_id'] == candidate.candidate_id
        assert json.loads(rows[-1]['details_json']) == dict(old_state=old_state, new_state=state)
        old_state = state
    other = store.create_matter('Other synthetic matter', '', ACTOR).matter_id
    before = review_rows(store), audit_rows(store)
    for scope, key in [(other, candidate.candidate_id), (matter, 'witness-inconsistency-' + '0' * 64)]:
        for method in (finder.dismiss, finder.mark_reviewed, finder.reopen):
            with pytest.raises(WorkspaceProblem):
                method(scope, ACTOR, key)
            assert (review_rows(store), audit_rows(store)) == before


def test_audit_failure_rolls_back_review(review_case, monkeypatch):
    store, matter, finder, _ = review_case
    candidate = finder.find(matter, ACTOR)[0]
    def fail_audit(**kwargs):
        assert store.connection.in_transaction
        raise ValueError('Synthetic audit failure')
    monkeypatch.setattr(finder.repository.entities, 'audit', fail_audit, raising=False)
    with pytest.raises(ValueError, match='Synthetic audit failure'):
        finder.dismiss(matter, ACTOR, candidate.candidate_id)
    assert review_rows(store) == [] and audit_rows(store) == []


def test_stored_review_can_be_reopened_after_source_removal(review_case):
    store, matter, finder, refs = review_case
    candidate = finder.find(matter, ACTOR)[0]
    finder.dismiss(matter, ACTOR, candidate.candidate_id)
    refs.pop(candidate.first.support_token)
    finder.reopen(matter, ACTOR, candidate.candidate_id)
    assert review_rows(store) == []
    assert candidate not in finder.find(matter, ACTOR)
    assert json.loads(audit_rows(store)[-1]['details_json'])['new_state'] == 'unreviewed'


def seed_accounts(store, finder, count, monkeypatch):
    sample = dict(store.connection.execute('SELECT * FROM workbench_assertion_account LIMIT 1').fetchone())
    rows = [dict(sample, account_id=f'synthetic-account-{index:04d}',
                 attributed_to=f'Synthetic witness {index}',
                 excerpt=f'At {index // 60:02d}:{index % 60:02d} beside the north gate.')
            for index in range(count)]
    # fixture-write: replace generated accounts with a bounded synthetic scan population.
    with fixture_write(store) as connection:
        connection.execute('DELETE FROM workbench_assertion_account')
        for row in rows:
            connection.execute('INSERT INTO workbench_assertion_account (' + ','.join(row) +
                ') VALUES (' + ','.join('?' for _ in row) + ')', tuple(row.values()))
    monkeypatch.setattr(finder.entities, 'validate_references', lambda values: frozenset(range(len(values))))
    return rows


@pytest.mark.parametrize('method', ['dismiss', 'mark_reviewed'])
def test_review_does_not_hold_writer_through_scan(review_case, tmp_path, monkeypatch, method):
    store, matter, finder, _ = review_case
    rows = seed_accounts(store, finder, 1000, monkeypatch)
    candidate = finder._candidate(matter, rows[-2], rows[-1], 'different witnesses on the same event', 'time')
    ready, stop = Event(), Event()
    def reserve_writer():
        successes, locked = 0, 0
        with sqlite3.connect(tmp_path / 'workspace.sqlite', timeout=0.2) as writer:
            while not stop.is_set():
                try:
                    # fixture-write: independent writer reservation probes the production scan lock.
                    writer.execute('BEGIN IMMEDIATE')
                    writer.rollback()
                    successes += 1
                except sqlite3.OperationalError as error:
                    assert 'locked' in str(error)
                    locked += 1
                ready.set()
                stop.wait(0.005)
        return successes, locked
    with ThreadPoolExecutor(max_workers=1) as pool:
        worker = pool.submit(reserve_writer)
        assert ready.wait(5)
        started = time.monotonic()
        try:
            getattr(finder, method)(matter, ACTOR, candidate.candidate_id)
        finally:
            elapsed = time.monotonic() - started
            stop.set()
        successes, locked = worker.result()
    print(f'{method}: accounts=1000 elapsed={elapsed:.2f}s writer_successes={successes} locked={locked}')
    assert elapsed < 60  # Generous CI bound for 499,500 candidate pairs.
    assert successes > 1 and locked == 0
    assert len(audit_rows(store)) == 1


def test_candidate_cap_reports_exact_omission_count(review_case, monkeypatch):
    store, matter, finder, _ = review_case
    seed_accounts(store, finder, 34, monkeypatch)
    result = finder.find(matter, ACTOR)
    assert len(result) == witness_module.MAX_CANDIDATES == 500
    assert result.not_shown == 34 * 33 // 2 - 500
    finder.dismiss(matter, ACTOR, result[0].candidate_id)
    assert finder.find(matter, ACTOR).not_shown == result.not_shown - 1
    assert len(finder.list_dismissed(matter, ACTOR)) == 1


def test_changed_account_between_scan_and_write_is_refused(review_case, monkeypatch):
    store, matter, finder, _ = review_case
    candidate = finder.find(matter, ACTOR)[0]
    scanned = []
    validate, transaction = finder.entities.validate_references, finder.repository.transaction
    def record_scan(rows):
        scanned.append(len(rows))
        return validate(rows)
    @contextmanager
    def change_before_write(matter_id, actor_id):
        assert scanned, 'The candidate scan must precede the write transaction'
        # fixture-write: simulate a concurrent account revision after the read snapshot ends.
        with fixture_write(store) as connection:
            connection.execute('UPDATE workbench_assertion_account SET attributed_to=? WHERE account_id=?',
                ('Changed synthetic attribution', candidate.first.account_id))
        with transaction(matter_id, actor_id) as repo:
            yield repo
    monkeypatch.setattr(finder.entities, 'validate_references', record_scan)
    monkeypatch.setattr(finder.repository, 'transaction', change_before_write)
    with pytest.raises(WorkspaceProblem):
        finder.dismiss(matter, ACTOR, candidate.candidate_id)
    assert review_rows(store) == [] and audit_rows(store) == []


def test_normalization_only_compares_accounts_on_same_event(review_case, monkeypatch):
    store, matter, finder, _ = review_case
    event = dict(store.connection.execute('SELECT * FROM workbench_assertion').fetchone())
    event['assertion_id'] = 'synthetic-separate-event'
    # fixture-write: partition four synthetic accounts into two events of two accounts each.
    with fixture_write(store) as connection:
        connection.execute('INSERT INTO workbench_assertion (' + ','.join(event) + ') VALUES (' +
            ','.join('?' for _ in event) + ')', tuple(event.values()))
        connection.execute('UPDATE workbench_assertion_account SET assertion_id=? WHERE support_token IN (?,?)',
            (event['assertion_id'], 'c' * 40, 'd' * 40))
    names = {'Avery Example', 'Blair Sample', 'Casey Fixture'}
    calls, normalize = [], witness_module._normalized
    def record_normalization(value):
        if value in names:
            calls.append(value)
        return normalize(value)
    monkeypatch.setattr(witness_module, '_normalized', record_normalization)
    finder.find(matter, ACTOR)
    assert len(calls) == 4


@pytest.mark.parametrize('guard', ['record_type', 'location', 'source_name'])
def test_event_and_nonblank_citation_guards(review_case, monkeypatch, guard):
    store, matter, finder, _ = review_case
    monkeypatch.setattr(finder.entities, 'validate_references', lambda rows: frozenset(range(len(rows))))
    assert finder.find(matter, ACTOR)
    # fixture-write: retain valid support while corrupting only the field guarded by this test.
    with fixture_write(store) as connection:
        if guard == 'record_type':
            connection.execute("UPDATE workbench_assertion SET record_type='assertion'")
        else:
            connection.execute('UPDATE workbench_assertion_account SET ' + guard + '=?', ('   ',))
    assert finder.find(matter, ACTOR) == ()


@pytest.mark.parametrize('method', ['dismiss', 'mark_reviewed'])
def test_review_enters_source_guard_once(review_case, monkeypatch, method):
    store, matter, finder, _ = review_case
    candidate = finder.find(matter, ACTOR)[0]
    entered = []
    @contextmanager
    def guard():
        entered.append(True)
        yield
    monkeypatch.setattr(finder.entities, 'source_guard', guard)
    getattr(finder, method)(matter, ACTOR, candidate.candidate_id)
    assert entered == [True]
