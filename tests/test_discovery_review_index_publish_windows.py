"""Synthetic parity and resource contracts for atomic project publication."""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, replace
import json
from types import SimpleNamespace

import pytest

from case_intelligence.modules.discovery_review import fields, load_files, reader
from case_intelligence.modules.discovery_review.production.repository import ProductionRepository
from case_intelligence.modules.discovery_review.stored_index.rebuild import Service
from case_intelligence.modules.discovery_review.stored_index.repository import Transaction, digest
from tests.fixture_write import fixture_write
from tests.test_discovery_review_index import add_population, drive, manifest, service_for
from tests.test_discovery_review_list import production as production
from tests.test_intake_receipt_http import ACTOR


def test_aliases_are_lazy_and_cached_by_definition_digest(monkeypatch):
    reader.header_aliases.cache_clear()
    definition = fields.load_definition()
    source = SimpleNamespace(document_id='synthetic-document', version_id='synthetic-version',
        display_name='record.txt', relative_path='Folder/record.txt', media_type='text/plain',
        page_count=0, size=1, has_text=False, state='queued', message='Queued for processing',
        origin='registered', digest='')
    match = load_files.Match('generated.csv', 2, 'path', {'BEGBATES': 'SYN000001'})
    expected = reader.project_item(source, definition, {}, match)
    calls = []
    original = reader.normalize_header

    def normalize(value):
        calls.append(value)
        return original(value)

    monkeypatch.setattr(reader, 'normalize_header', normalize)
    key = digest(asdict(definition))
    reader.project_item(source, definition, {}, definition_digest=key)
    assert calls == []
    assert reader.project_item(source, definition, {}, match, definition_digest=key) == expected
    first = len(calls)
    assert first > len(match.values)
    calls.clear()
    equivalent = replace(definition)
    assert reader.project_item(source, equivalent, {}, match, definition_digest=key) == expected
    assert len(calls) == len(match.values)
    calls.clear()
    reader.project_item(source, equivalent, {}, match)
    assert len(calls) == first
    calls.clear()
    reader.project_item(source, equivalent, {}, match, definition_digest=key + '-distinct')
    assert len(calls) == first


def index_rows(service, mid):
    return {suffix: service.repository.rows('SELECT * FROM workbench_discovery_index_' + suffix
        + ' WHERE matter_id=? ORDER BY 1,2', (mid,)) for suffix in ('item', 'value', 'state', 'load_row')}


def restore_index(bench, mid, saved):
    with fixture_write(bench.workspace) as connection:
        for suffix in ('value', 'load_row', 'item', 'state'):
            connection.execute('DELETE FROM workbench_discovery_index_' + suffix + ' WHERE matter_id=?', (mid,))
        for suffix, rows in saved.items():
            if rows:
                slots = ','.join('?' for _ in rows[0])
                connection.executemany('INSERT INTO workbench_discovery_index_' + suffix + ' VALUES (' + slots + ')', rows)


@contextmanager
def bounded_reads(monkeypatch):
    original_read, original_rows = Transaction.read, ProductionRepository.projection_rows
    original_primitive, original_step = Service.primitive, Service.step
    observed = []

    def read(tx, *args, **kwargs):
        before = tx.budget.touches
        rows = original_read(tx, *args, **kwargs)
        assert len(rows) <= 24
        assert tx.budget.touches - before <= 24
        observed.append(len(rows))
        return rows

    def rows(repo, *args, **kwargs):
        budget = repo.budget()
        before = budget.touches if budget else 0
        found = original_rows(repo, *args, **kwargs)
        assert len(found) <= 24
        if budget:
            assert budget.touches - before <= 24
        observed.append(len(found))
        return found

    def primitive(service, tx, job, cursor, *args):
        before, phase = tx.budget.work, cursor['phase']
        result = original_primitive(service, tx, job, cursor, *args)
        if phase in ('project', 'order'):
            assert tx.budget.work - before <= 24
        return result

    def step(service, job, budget, *args):
        result = original_step(service, job, budget, *args)
        assert budget.touches <= 256
        return result

    with monkeypatch.context() as patch:
        patch.setattr(Transaction, 'read', read)
        patch.setattr(ProductionRepository, 'projection_rows', rows)
        patch.setattr(Service, 'primitive', primitive)
        patch.setattr(Service, 'step', step)
        yield observed


def test_windows_match_single_path_with_complete_coding_pages(production, monkeypatch):
    client, bench, matter = production
    service = service_for(bench)
    ids = sorted(add_population(bench.source_store(matter), 32))
    codes = [service.codes.define(matter.matter_id, ACTOR, f'Synthetic {i:02}') for i in range(15)]
    for document in ids:
        for code in codes[:3]:
            service.codes.record(matter.matter_id, document, code.code_id, ACTOR, 'apply')
    for code in codes:
        service.codes.record(matter.matter_id, ids[15], code.code_id, ACTOR, 'apply')
    service.codes.retire(matter.matter_id, codes[1].code_id)
    dimensions = {**manifest(36), 'C': len(service.codes.current_rows(matter.matter_id))}
    saved = {}
    original_step = Service.step

    def step(candidate, job, budget, *args):
        state = candidate.repository.state(job.matter_id)
        if state and state['phase'] == 'project' and not saved:
            saved.update(index_rows(candidate, job.matter_id))
        return original_step(candidate, job, budget, *args)

    with monkeypatch.context() as patch:
        patch.setattr(Service, 'prepare_window', lambda *args: {'window_checked': True})
        patch.setattr(Service, 'step', step)
        drive(production, dimensions)
    assert saved
    expected = index_rows(service, matter.matter_id)
    restore_index(bench, matter.matter_id, saved)
    windows = []
    publish = service.repository.publish_many

    def publish_many(tx, mid, publications, generation):
        windows.append([item.document_id for item, fingerprint, cache in publications])
        return publish(tx, mid, publications, generation)

    monkeypatch.setattr(service.repository, 'publish_many', publish_many)
    with bounded_reads(monkeypatch) as observed:
        drive(production, dimensions)
    actual = index_rows(service, matter.matter_id)
    for actual_row, expected_row in zip(actual['item'], expected['item']):
        for column, (actual_value, expected_value) in enumerate(zip(actual_row, expected_row)):
            assert actual_value == expected_value, (actual_row[1], column, actual_value, expected_value)
    assert actual['item'] == expected['item']
    assert sorted(actual['value']) == sorted(expected['value'])
    assert windows and max(map(len, windows)) <= 12
    assert all(ids[15] not in window for window in windows)
    assert any(1 <= len(set(ids) & set(window)) < 4 for window in windows)
    assert observed


def test_family_windows_obey_read_and_step_caps(tmp_path, monkeypatch):
    from tests.test_discovery_review_dat_opt_storage import app_case, finish_import, complete_http_page
    with app_case(tmp_path, profile='family', count=24, width=128) as case:
        finish_import(case)
        with bounded_reads(monkeypatch) as observed:
            complete_http_page(case, case.matter, load_records=25)
        assert observed


def test_window_stops_before_a_projection_error(production, monkeypatch):
    client, bench, matter = production
    service = service_for(bench)
    add_population(bench.source_store(matter), 32)
    target = None
    batch, single = service.production.document_projections, service.production.document_projection
    selected, singles, windows = [], [], []
    original_publish = service.repository.publish_many

    def projections(mid, pairs):
        nonlocal target
        if target is None and len(pairs) >= 4:
            target = pairs[2][0]
        selected.append([pair[0] for pair in pairs])
        if target in selected[-1]:
            raise KeyError(target)
        return batch(mid, pairs)

    def projection(mid, document, version):
        singles.append(document)
        if document == target:
            raise KeyError(target)
        return single(mid, document, version)

    def publish(tx, mid, publications, generation):
        windows.append([item.document_id for item, fingerprint, cache in publications])
        return original_publish(tx, mid, publications, generation)

    monkeypatch.setattr(service.production, 'document_projections', projections)
    monkeypatch.setattr(service.production, 'document_projection', projection)
    monkeypatch.setattr(service.repository, 'publish_many', publish)
    with pytest.raises(KeyError) as error:
        drive(production, manifest(36))
    assert error.value.args == (target,)
    assert any(target in pairs for pairs in selected)
    assert target in singles
    assert all(target not in window for window in windows)
    assert any(window[-1] < target for window in windows)


def test_cancel_and_reopen_after_atomic_publication(production, monkeypatch):
    client, bench, matter = production
    service = service_for(bench)
    add_population(bench.source_store(matter), 36)
    saved = {}
    original = Service.step

    def step(candidate, job, budget, *args):
        state = candidate.repository.state(job.matter_id)
        if state and state['phase'] == 'project' and not saved:
            saved.update(index_rows(candidate, job.matter_id))
        return original(candidate, job, budget, *args)

    with monkeypatch.context() as patch:
        patch.setattr(Service, 'step', step)
        drive(production, manifest(40))
    assert saved
    expected = index_rows(service, matter.matter_id)
    restore_index(bench, matter.matter_id, saved)
    before = json.loads(saved['state'][0][-1])['after_document']
    job = service.jobs[matter.matter_id]
    for _ in range(20):
        service.advance(job, 2)
        assert not job.error, getattr(service, 'failure', '')
        cursor = json.loads(service.repository.state(matter.matter_id)['checkpoint'])
        if cursor['after_document'] != before and cursor['active'] is None:
            assert cursor['phase'] == 'project'
            break
    else:
        raise AssertionError(('no atomic project window was committed', cursor['phase'], cursor['after_document'], cursor['active'], job.attempts))
    service.cancel(matter.matter_id)
    drive(production, manifest(40))
    actual = index_rows(service, matter.matter_id)
    assert sorted(actual['value']) == sorted(expected['value'])
    assert actual['item'] == expected['item']
    assert len(actual['item']) == len({row[1] for row in actual['item']}) == 40


def test_coding_resume_enters_the_existing_single_record_path():
    service = object.__new__(Service)
    service.prepare_window = lambda *args: (_ for _ in ()).throw(AssertionError('coding resume entered a window'))
    cursor = {'phase': 'project', 'active': None}
    assert service.prepare(None, cursor, None, {}, {'coding_resume': {'active': {}}}) == {'window_checked': True}


@pytest.mark.parametrize('profile', ['email', 'load-file'])
def test_single_path_project_steps_and_commits_match_base(tmp_path, monkeypatch, profile):
    from tests.test_discovery_review_dat_opt_storage import app_case, complete_http_page
    from tests.test_discovery_review_list import EMAIL

    population = 25
    suffix, content = ('eml', EMAIL) if profile == 'email' else ('csv', b'BEGBATES,NATIVEPATH\r\n')
    files = [(f'record-{number:04}.{suffix}', content) for number in range(population - 1)]
    with app_case(tmp_path, count=0, extra_files=files) as case:
        service = service_for(case.bench)
        in_project = ContextVar('in_project', default=False)
        counts = {'steps': 0, 'commits': 0}
        original = Service.step

        def trace(statement):
            if in_project.get() and statement.strip().upper() == 'COMMIT':
                counts['commits'] += 1

        def step(candidate, job, budget, *args):
            state = candidate.repository.state(job.matter_id)
            project = job.matter_id == case.matter.matter_id and state and state['phase'] == 'project'
            token = in_project.set(project)
            counts['steps'] += bool(project)
            try:
                return original(candidate, job, budget, *args)
            finally:
                in_project.reset(token)

        monkeypatch.setattr(Service, 'step', step)
        with case.workspace._lock:
            case.workspace.connection.set_trace_callback(trace)
        try:
            complete_http_page(case, case.matter, load_records=0)
        finally:
            with case.workspace._lock:
                case.workspace.connection.set_trace_callback(None)
        assert service.repository.state(case.matter.matter_id)['phase'] == 'complete'
        # Measured on dafce68 with this fixture; see PR.md for the baseline run.
        assert 0 < counts['steps'] / population <= 1.0, counts
        assert 0 < counts['commits'] / population <= 1.0, counts
