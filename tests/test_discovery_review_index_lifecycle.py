"""Synthetic request ownership, cancellation and derived-state lifecycle proofs."""
from dataclasses import replace
import json
import threading
import traceback

import pytest

from tests.fixture_write import fixture_write
from tests.test_discovery_review_list import production as production, token_for
from tests.test_discovery_review_index import add_population, drive, manifest, service_for
from tests.test_discovery_review_index_updates import pending_job
from tests.test_matter_archive import archive_case as archive_case, OWNER, close, records


TABLES = tuple('workbench_discovery_index_' + suffix for suffix in ('value', 'load_row', 'item', 'state'))


def test_no_background_thread_touches_shared_connection_after_a_coding_post(request, monkeypatch):
    from case_intelligence.modules.discovery_review.stored_index.rebuild import Service
    starts, intervals, sql = [], set(), []
    original_start, original_step = threading.Thread.start, Service.step
    def start(thread):
        target = getattr(thread, '_target', None)
        starts.append((thread.name, getattr(target, '__module__', ''), traceback.extract_stack()))
        return original_start(thread)
    def step(service, *args, **kwargs):
        assert intervals, 'index execution outside a complete client-call interval'
        return original_step(service, *args, **kwargs)
    monkeypatch.setattr(threading.Thread, 'start', start)
    monkeypatch.setattr(Service, 'step', step)
    client, bench, matter = request.getfixturevalue('production')
    def assert_origins():
        assert not any(thread.name == 'discovery-index' for thread in threading.enumerate())
        for name, module, stack in starts:
            assert name != 'discovery-index' and 'modules.discovery_review' not in module
            assert not any('/modules/discovery_review/' in frame.filename for frame in stack)
    assert_origins()
    def trace(statement):
        if 'workbench_discovery_index_' in statement:
            sql.append((statement, threading.current_thread().name, tuple(intervals)))
    with bench.workspace._lock:
        bench.workspace.connection.set_trace_callback(trace)
    def call(method, url, **kwargs):
        intervals.add('synthetic-call')
        try:
            return getattr(client, method)(url, **kwargs)
        finally:
            intervals.remove('synthetic-call')
    base = f'/matters/{matter.slug}/production'
    try:
        assert call('get', base + '?view=fields').status_code == 200
        assert_origins()
        response = call('post', base + '/codes', data={'label': 'Synthetic label'})
        assert response.status_code == 200
        code = service_for(bench).codes.list(matter.matter_id)[0]
        token = token_for(bench, matter, 'memo.txt')
        assert call('post', base + '/items/' + token + '/codes/' + code.code_id, data={'action': 'apply'}).status_code == 200
        assert_origins()
        assert call('get', base + '?view=fields').status_code == 200
        assert sql and all(active for statement, thread, active in sql)
        assert_origins()
    finally:
        with bench.workspace._lock:
            bench.workspace.connection.set_trace_callback(None)


def test_pending_index_advances_in_bounded_steps_per_get(production):
    import math
    from case_intelligence.modules.discovery_review.stored_index.rebuild import work_bound
    client, bench, matter = production
    add_population(bench.source_store(matter), 996)
    service = service_for(bench)
    previous, saw_pending = -1, False
    for _ in range(math.ceil(work_bound(**manifest(1000))[2] / 128)):
        page = client.get(f'/matters/{matter.slug}/production?view=fields')
        state = service.repository.state(matter.matter_id)
        cursor = json.loads(state['checkpoint'])
        assert service.jobs[matter.matter_id].attempts <= 128
        assert cursor['sequence'] > previous
        previous = cursor['sequence']
        if state['phase'] == 'complete':
            assert all(cursor['eof'].values()) and state['indexed'] == 1000
            break
        saw_pending = True
        assert 'while indexing' in page.text and state['pending'] > 0
        assert 'No documents match this filter.' not in page.text
    else:
        pytest.fail('stable target did not complete within manifest')
    assert saw_pending


class OwnershipProbe:
    def __init__(self):
        self.lock = threading.Lock()
        self.attempts = []
        self.lost = threading.Event()
        self.release_owner = threading.Event()
        self.pause_release = False

    def acquire(self, blocking=True):
        assert blocking is False
        result = self.lock.acquire(blocking=False)
        self.attempts.append((threading.get_ident(), blocking, result))
        if not result:
            self.lost.set()
        return result

    def release(self):
        if self.pause_release:
            assert self.release_owner.wait(20)
        self.lock.release()


def test_concurrent_request_never_waits_for_generator_ownership(production, monkeypatch):
    client, bench, matter = production
    service = service_for(bench)
    from contextlib import contextmanager
    ownership = OwnershipProbe()
    monkeypatch.setattr(service, 'step_lock', ownership)
    entered, release_storage, results, failures = threading.Event(), threading.Event(), [], []
    owner, storage_attempt, snapshot = [], threading.Event(), threading.Event()
    store = bench.source_store(matter)
    guard = store.mutation_guard
    @contextmanager
    def observed_guard():
        if owner and threading.get_ident() != owner[0]:
            storage_attempt.set()
        with guard():
            yield
    monkeypatch.setattr(store, 'mutation_guard', observed_guard)
    database_lock = bench.workspace._lock
    class ObservedDatabaseLock:
        def _is_owned(self):
            return database_lock._is_owned()
        def acquire(self, *args, **kwargs):
            if owner and threading.get_ident() != owner[0]:
                storage_attempt.set()
            return database_lock.acquire(*args, **kwargs)
        def release(self):
            database_lock.release()
        def __enter__(self):
            self.acquire()
            return self
        def __exit__(self, *args):
            self.release()
    monkeypatch.setattr(bench.workspace, '_lock', ObservedDatabaseLock())
    state = service.repository.state
    def observed_state(*args, **kwargs):
        result = state(*args, **kwargs)
        if owner and threading.get_ident() != owner[0] and ownership.lost.is_set():
            snapshot.set()
        return result
    monkeypatch.setattr(service.repository, 'state', observed_state)
    save = service.repository.save
    def paused_save(*args, **kwargs):
        result = save(*args, **kwargs)
        if not owner:
            owner.append(threading.get_ident())
        entered.set()
        assert release_storage.wait(20)
        ownership.pause_release = True
        return result
    monkeypatch.setattr(service.repository, 'save', paused_save)
    def request_page():
        try:
            results.append(client.get(f'/matters/{matter.slug}/production'))
        except BaseException as error:
            failures.append(error)
    first = threading.Thread(target=request_page)
    second = threading.Thread(target=request_page)
    first.start()
    try:
        assert entered.wait(20), failures
        second.start()
        assert storage_attempt.wait(20)
        assert not ownership.lost.is_set()
        release_storage.set()
        assert ownership.lost.wait(20)
        assert snapshot.wait(20)
        successes, losses = 0, 0
        for thread, blocking, success in ownership.attempts:
            successes += int(success)
            losses += int(not success)
        assert successes == losses == 1
    finally:
        release_storage.set()
        ownership.release_owner.set()
        first.join(20)
        if second.ident:
            second.join(20)
    assert not first.is_alive() and not second.is_alive() and not failures
    assert len(results) == 2 and all(result.status_code == 200 for result in results)


def test_capacity_precedes_step_lock_and_cancel_never_takes_ownership(production, monkeypatch):
    service, job = pending_job(production, monkeypatch)
    capacity, observations = service.context.ensure_capacity, []
    def preflight(size):
        assert not service.context.db.lock._is_owned()
        assert not job.store._lock._is_owned()
        assert service.step_lock.acquire(blocking=False)
        service.step_lock.release()
        assert service.jobs_lock.acquire(blocking=False)
        service.jobs_lock.release()
        observations.append(size)
        capacity(size)
    monkeypatch.setattr(service, 'context', replace(service.context, ensure_capacity=preflight))
    service.advance(job, 1)
    assert observations == [2 * 1024 * 1024]
    assert service.step_lock.acquire(blocking=False)
    try:
        service.cancel(job.matter_id)
        assert job.cancelled and job.matter_id not in service.jobs
    finally:
        service.step_lock.release()
    state = service.repository.state(job.matter_id)
    service.advance(job, 1)
    assert service.repository.state(job.matter_id) == state


def test_cursor_purge_cancel_rejects_prepared_step(production, monkeypatch):
    from tests.case_lifecycle_web_helpers import claim_bench_exit
    service, job = pending_job(production, monkeypatch)
    client, bench, matter = production
    service.advance(job, 1)
    ready, release, errors = threading.Event(), threading.Event(), []
    prepare = service.prepare
    def paused(*args):
        result = prepare(*args)
        ready.set()
        assert release.wait(20)
        return result
    monkeypatch.setattr(service, 'prepare', paused)
    def advance():
        try:
            service.advance(job, 1)
        except BaseException as error:
            errors.append(error)
    worker = threading.Thread(target=advance)
    worker.start()
    try:
        assert ready.wait(20)
        service.cancel(matter.matter_id)
        prepared, lifecycle = claim_bench_exit(bench, matter)
        assert bench.execute_matter_purge(prepared, lifecycle).state == 'deleted'
    finally:
        release.set()
        worker.join(20)
    assert not worker.is_alive() and not errors
    for table in TABLES:
        assert not service.repository.rows(f'SELECT matter_id FROM {table} WHERE matter_id=?', (matter.matter_id,))


def test_purge_retry_and_late_step_leave_no_rows(production, monkeypatch):
    from tests.case_lifecycle_web_helpers import claim_bench_exit
    client, bench, matter = production
    drive(production, manifest(4))
    service = service_for(bench)
    job = service.jobs[matter.matter_id]
    from tests.test_intake_receipt_http import ACTOR
    from tests.test_discovery_review_list import add_files
    from case_intelligence.modules.discovery_review.purge import purge_hook
    response = client.post('/matters', data={'name': 'Synthetic neighbor', 'descriptor': 'Generated'}, follow_redirects=False)
    neighbor = bench.matter(response.headers['location'].split('/')[2], ACTOR)
    add_files(client, bench, neighbor, {'neighbor.txt': b'Synthetic neighboring matter.'}, key='b')
    assert client.get(f'/matters/{neighbor.slug}/production').status_code == 200
    retained = {table: service.repository.rows(f'SELECT * FROM {table} WHERE matter_id=?', (neighbor.matter_id,)) for table in TABLES}
    assert retained[TABLES[-1]] and retained[TABLES[-2]]
    root = bench.storage.matters / matter.matter_id
    purge_hook(root, matter.matter_id)
    purge_hook(root, matter.matter_id)
    prepared, lifecycle = claim_bench_exit(bench, matter)
    assert bench.execute_matter_purge(prepared, lifecycle).state == 'deleted'
    service.cancel(matter.matter_id)
    service.advance(job, 1)
    for table in TABLES:
        assert not service.repository.rows(f'SELECT matter_id FROM {table} WHERE matter_id=?', (matter.matter_id,))
    assert client.get(f'/matters/{matter.slug}/production').status_code == 404
    assert {table: service.repository.rows(f'SELECT * FROM {table} WHERE matter_id=?', (neighbor.matter_id,)) for table in TABLES} == retained


def test_archive_excludes_index_preserves_human_work(archive_case):
    import io
    import zipfile
    from types import SimpleNamespace
    from case_intelligence.modules import ModuleDatabase
    from case_intelligence.modules.discovery_review import fields
    from case_intelligence.modules.discovery_review.codes import CodeRepository
    from case_intelligence.modules.discovery_review.identifiers import IdentifierRepository
    from case_intelligence.modules.discovery_review.stored_index.rebuild import Service
    from case_intelligence.matter_archive import build_matter_archive, cleanup_archive, matter_snapshot_digest
    bench, matter, store = archive_case
    document, _ = store.store_stream('synthetic.txt', 'text/plain', io.BytesIO(b'Synthetic text'))
    db = ModuleDatabase(bench.workspace.connection, bench.workspace._lock)
    identifiers, codes = IdentifierRepository(db, bench.workspace.membership), CodeRepository(db)
    identifiers.assign(matter.matter_id, OWNER, [(document.document_id, 'SYN001')])
    code = codes.define(matter.matter_id, OWNER, 'Synthetic code')
    codes.record(matter.matter_id, document.document_id, code.code_id, OWNER, 'apply')
    service = Service(SimpleNamespace(db=db, ensure_capacity=bench.storage.ensure_capacity), identifiers, codes)
    before = matter_snapshot_digest(bench, matter, store)
    assert service.current(matter.matter_id, store, fields.load_definition()).complete
    assert matter_snapshot_digest(bench, matter, store) == before
    close(bench, matter)
    artifact = build_matter_archive(bench, matter, OWNER, source_store=store)
    try:
        with zipfile.ZipFile(artifact.path) as archive:
            assert not any('records/' + table + '.jsonl' in archive.namelist() for table in TABLES)
        assert records(artifact, 'workbench_discovery_identifier')[0]['identifier'] == 'SYN001'
        assert records(artifact, 'workbench_discovery_code_event')[0]['action'] == 'apply'
    finally:
        cleanup_archive(artifact)


def test_unknown_schema_still_blocks_archive(archive_case):
    from case_intelligence.matter_archive_schema import validate_schema
    bench, matter, store = archive_case
    workspace = bench.workspace
    validate_schema(workspace.connection)
    with fixture_write(workspace) as connection:
        connection.execute('ALTER TABLE workbench_discovery_index_state ADD COLUMN synthetic_unknown TEXT')
    with pytest.raises(Exception, match='schema|column|reviewed'):
        validate_schema(workspace.connection)


def test_frozen_matter_rejects_index_writes(production):
    from tests.case_lifecycle_helpers import lifecycle_session
    client, bench, matter = production
    drive(production, manifest(4))
    service, workspace = service_for(bench), bench.workspace
    session = lifecycle_session(workspace, matter.owner_id)
    workspace.case_lifecycle.begin_exit(matter.slug, matter.owner_id, session_id=session.session_id)
    assert len(service.repository.rows('SELECT document_id FROM workbench_discovery_index_item WHERE matter_id=?', (matter.matter_id,))) == 4
    for table in TABLES:
        for statement in (f'DELETE FROM {table} WHERE matter_id=?',
                          f'UPDATE {table} SET matter_id=matter_id WHERE matter_id=?',
                          f'INSERT INTO {table} SELECT * FROM {table} WHERE matter_id=?'):
            with pytest.raises(Exception, match='frozen|active|closing|content'):
                with fixture_write(workspace) as connection:
                    connection.execute(statement, (matter.matter_id,))
    before = service.repository.state(matter.matter_id)
    service.invalidate(matter.matter_id)
    service.advance(service.jobs[matter.matter_id], 1)
    assert service.repository.state(matter.matter_id) == before


def test_migration_mirror_and_clean_restore_rebuild(production, tmp_path):
    from pathlib import Path
    import sqlite3
    from case_intelligence.workspace_store import WorkspaceStore
    from case_intelligence.modules import ModuleDatabase
    from case_intelligence.modules.discovery_review.codes import CodeRepository
    from case_intelligence.modules.discovery_review.identifiers import IdentifierRepository
    from case_intelligence.modules.discovery_review import fields
    from case_intelligence.modules.discovery_review.stored_index.rebuild import Service
    client, bench, matter = production
    drive(production, manifest(4))
    service = service_for(bench)
    root = Path(__file__).resolve().parents[1]
    name = 'migrations/sqlite/0049_discovery_review_index.sql'
    assert (root / name).read_bytes() == (root / 'src/case_intelligence' / name).read_bytes()
    with sqlite3.connect(tmp_path / 'restored.sqlite') as backup:
        with bench.workspace._lock:
            bench.workspace.connection.backup(backup)
    restored = WorkspaceStore(tmp_path / 'restored.sqlite')
    try:
        with fixture_write(restored) as connection:
            for table in TABLES:
                connection.execute(f'DELETE FROM {table}')
        db = ModuleDatabase(restored.connection, restored._lock)
        rebuilt = Service(replace(service.context, db=db), IdentifierRepository(db, service.context.membership), CodeRepository(db))
        view = rebuilt.current(matter.matter_id, bench.source_store(matter), fields.load_definition())
        assert view.complete and view.total == 4
        assert rebuilt.repository.rows('SELECT document_id,payload FROM workbench_discovery_index_item ORDER BY document_id') == service.repository.rows(
            'SELECT document_id,payload FROM workbench_discovery_index_item ORDER BY document_id')
    finally:
        restored.close()


def test_single_heavily_coded_item_stays_pending_with_complete_values_on_resume(production, monkeypatch):
    from tests.test_intake_receipt_http import ACTOR
    from case_intelligence.modules.discovery_review.stored_index.query import lookup
    from case_intelligence.modules.discovery_review import fields
    client, bench, matter = production
    store = bench.source_store(matter)
    document_id = next(iter(store.documents))
    workspace = bench.workspace
    population = 15_000
    with fixture_write(workspace) as connection:
        for number in range(population):
            code_id = f'synthetic-{number:06}'
            connection.execute('INSERT INTO workbench_discovery_code VALUES (?,?,?,?,?,?,?,?)',
                               (code_id, matter.matter_id, f'Tag {number:06}', number + 1, None, 1, ACTOR, '2025-01-01T00:00:00Z'))
            connection.execute('INSERT INTO workbench_discovery_code_event VALUES (?,?,?,?,?,?,?)',
                               (number + 1, matter.matter_id, document_id, code_id, 'apply', ACTOR, '2025-01-01T00:00:00Z'))
    service = service_for(bench)
    capacity, reservations = service.context.ensure_capacity, []
    def reserve(size):
        assert not service.context.db.lock._is_owned() and not store._lock._is_owned()
        assert service.step_lock.acquire(blocking=False)
        service.step_lock.release()
        reservations.append(size)
        capacity(size)
    monkeypatch.setattr(service, 'context', replace(service.context, ensure_capacity=reserve))
    view = service.current(matter.matter_id, store, fields.load_definition())
    assert not view.complete
    item = lookup(view, store.action_token(store.documents[document_id]))
    assert item.metadata_state == 'pending'
    from case_intelligence.modules.discovery_review.stored_index.rebuild import work_bound, MIN_CAPACITY
    from case_intelligence.modules.discovery_review.stored_index.repository import packed
    import math
    dimensions = {**manifest(4), 'C': population, 'P': population // 8 + 5}
    encoded_upper = len(packed([{'value': f'Tag {number:06}', 'active': True,
                                'created_by': ACTOR, 'created_at': '2025-01-01T00:00:00Z'}
                               for number in range(population)]).encode()) * 4 + 65536
    _, _, base_steps = work_bound(**dimensions)
    preparations = base_steps * min(224, math.ceil(224 * (encoded_upper * 4 + 4096) / MIN_CAPACITY))
    page = drive(production, {**dimensions, 'U': preparations * 224, 'Q': preparations})
    assert max(reservations) > MIN_CAPACITY
    assert page.status_code == 200
    view = service.current(matter.matter_id, store, fields.load_definition())
    item = lookup(view, item.token)
    assert item.metadata_state == 'complete'
    values = next(value for key, value in item.values.items() if 'Tag 000000' in value)
    assert values.count('Tag ') == population and 'Tag 014999' in values
