"""Synthetic regressions for useful rebuild steps and version-two key bytes."""
from dataclasses import asdict, replace
import json
from types import SimpleNamespace

import pytest

from case_intelligence.modules.discovery_review import fields, reader
from case_intelligence.modules.discovery_review.stored_index import keys
from case_intelligence.modules.discovery_review.stored_index.models import Budget
from case_intelligence.modules.discovery_review.stored_index.rebuild import Service
from case_intelligence.modules.discovery_review.stored_index.repository import Repository, Transaction
from tests.test_discovery_review_index import add_population, drive, manifest, service_for
from tests.test_discovery_review_list import production as production


def old_encode(value):
    if isinstance(value, tuple):
        return b'\x03' + b''.join(old_encode(part) for part in value) + b'\x00'
    if isinstance(value, int):
        if value < 0:
            raise ValueError('ordering numbers must be nonnegative')
        digits = str(value).encode('ascii')
        return b'\x01' + len(digits).to_bytes(4, 'big') + digits
    return b'\x02' + b''.join((ord(character) + 1).to_bytes(3, 'big') for character in value) + b'\x00\x00\x00'


def old_order(chosen, item, descending, parent=None, components=None):
    family = components is not None and item.family.get('root')
    anchor = (parent or item) if family else item
    tie = (*reader.tie_key(anchor), anchor.version_id)
    value = components['group'] if family else reader.sort_key(chosen, item)
    complete = old_encode(tie) if value is None else old_encode((value, tie))
    if descending and value is not None:
        complete = bytes(255 - byte for byte in complete)
    return ((b'\x01' if value is None else b'\x00') + complete
            + (old_encode(components['member']) if family else b''))


def sample_item(label='ABC02', document='synthetic-1'):
    return reader.Item(document, document, 'version-1', 'document', 'text/plain', '', False,
                       {'name': label, 'path': 'Folder/record-002', 'size': '1,234'}, {}, {})


def test_population_registration_uses_one_atomic_batch():
    batches = []

    def register(*, source_location_id, sources):
        batches.append((source_location_id, sources))
        return [SimpleNamespace(document_id=f'synthetic-{source.stable_inode}') for source in sources]

    identities = add_population(SimpleNamespace(register_linked_sources=register), 201, prefix='Synthetic')
    assert identities == [f'synthetic-{number}' for number in range(201)]
    assert len(batches) == 1
    location, sources = batches[0]
    assert location == 'synthetic-index'
    assert [source.relative_path for source in sources] == [f'Synthetic/record-{number}.txt' for number in range(201)]
    assert all(source.media_type == 'text/plain' and source.byte_size == 1 and source.stable_device == 0
               and source.stable_mtime_ns == 0 for source in sources)


def test_unmatched_projection_does_not_normalize_unused_header_aliases(monkeypatch):
    def unused(header):
        pytest.fail('unmatched projection normalized unused load-file aliases')

    monkeypatch.setattr(reader, 'normalize_header', unused)
    source = SimpleNamespace(document_id='synthetic-document', version_id='synthetic-version',
                             display_name='record.txt', relative_path='Folder/record.txt',
                             media_type='text/plain', page_count=0, size=1, has_text=False,
                             state='queued', message='Queued for processing', origin='registered', digest='')
    item = reader.project_item(source, fields.load_definition(), {})
    assert item.value('name') == 'record.txt'
    assert item.value('path') == 'Folder/record.txt'
    assert item.load_file == {} and item.kind == 'document'


@pytest.mark.parametrize('descending', [False, True])
@pytest.mark.parametrize('label', ['ABC2', 'ABC02', '', ' ', 'Straße\x00\U0010ffff', '9' * 2000])
def test_persisted_key_bytes_match_version_two(monkeypatch, descending, label):
    definition = fields.load_definition()
    parent = sample_item('Parent10', 'parent')
    item = sample_item(label)
    stored = []
    tx = Transaction(None, Budget(256, 224), 2 * 1024 * 1024)
    monkeypatch.setattr(tx, 'write', lambda statement, args: stored.append(args))
    repo = Repository(SimpleNamespace(connection=None))
    for candidate in (item, replace(item, document_id='synthetic-2'),
                      replace(item, family={'root': 'parent', 'order': '02'})):
        for chosen in (fields.Field('', '', '', sort='natural'), definition.field('name'),
                       definition.field('size'), definition.field('custodian')):
            components = reader.production_order_components(definition, candidate, parent, chosen.key)
            repo.value(tx, 'synthetic-matter', candidate, chosen, descending, components=components, parent=parent)
            expected = old_order(chosen, candidate, descending, parent, components)
            assert stored[-1][4:6] == (expected[:keys.PREFIX_BYTES], expected)
    assert keys.VERSION == 2


def test_value_encoding_is_reused_only_within_the_transaction(monkeypatch):
    original = keys.encode
    calls = []

    def counted(value):
        calls.append(value)
        return original(value)

    monkeypatch.setattr(keys, 'encode', counted)
    repo = Repository(SimpleNamespace(connection=None))
    item = sample_item()
    for matter in ('first', 'second'):
        tx = Transaction(None, Budget(256, 224), 2 * 1024 * 1024)
        monkeypatch.setattr(tx, 'write', lambda statement, args: None)
        chosen = fields.Field('absent', '', '', sort='natural')
        repo.value(tx, matter, item, chosen, False)
        first = len(calls)
        assert first > 0
        calls.clear()
        repo.value(tx, matter, item, chosen, True)
        repo.value(tx, matter, item, fields.Field('also_absent', '', '', sort='natural'), False)
        assert calls == []


def test_preparation_batches_documents_and_commits_each_step_once(production, monkeypatch):
    client, bench, matter = production
    add_population(bench.source_store(matter), 96)
    steps, commits = [], []
    original_step, original_transaction = Service.step, Repository.transaction

    def transaction(repo, *args):
        commits.append(repo)
        return original_transaction(repo, *args)

    def step(service, job, budget, *args):
        before = len(commits)
        result = original_step(service, job, budget, *args)
        steps.append(budget.work)
        assert len(commits) - before == 1
        assert budget.touches <= 256
        return result

    monkeypatch.setattr(Repository, 'transaction', transaction)
    monkeypatch.setattr(Service, 'step', step)
    drive(production, manifest(100))
    service_for(bench).invalidate(matter.matter_id)
    drive(production, manifest(100))
    assert len(steps) <= 90


def test_order_inserts_bounded_value_batches_with_exact_row_charges(production, monkeypatch):
    inserted = []
    original = Transaction.write
    original_components = reader.production_order_components

    def family_components(definition, item, parent, sort):
        assert item.family.get('root')
        return original_components(definition, item, parent, sort)

    def write(tx, statement, args=(), **kwargs):
        before = tx.budget.touches
        affected = original(tx, statement, args, **kwargs)
        if statement.startswith('INSERT INTO workbench_discovery_index_value '):
            assert tx.budget.touches - before == affected
            if kwargs.get('many'):
                assert len(args) == affected and all(len(row) == 8 for row in args)
            else:
                assert len(args) == 8 * affected
            inserted.append(affected)
        return affected

    monkeypatch.setattr(Transaction, 'write', write)
    monkeypatch.setattr(reader, 'production_order_components', family_components)
    drive(production, manifest(4))
    client, bench, matter = production
    repo = service_for(bench).repository
    assert repo.rows('SELECT COUNT(*) FROM workbench_discovery_index_value WHERE matter_id=?',
                     (matter.matter_id,)) == ((312,),)
    assert sum(inserted) == 312
    assert max(inserted) <= 20
    assert len(inserted) <= 24
    definition = fields.load_definition()
    for document, key, direction, actual in repo.rows(
            'SELECT document_id,field_key,direction,sort_key FROM workbench_discovery_index_value WHERE matter_id=?',
            (matter.matter_id,)):
        item = reader.Item(**repo.full_item(matter.matter_id, document))
        chosen = definition.field(key) if key else fields.Field('', '', '', sort='natural')
        assert actual == old_order(chosen, item, bool(direction))


def test_unassociated_projection_matches_full_preparation(production, monkeypatch):
    client, bench, matter = production
    store = bench.source_store(matter)
    generated = add_population(store, 1)
    drive(production, manifest(5))
    service = service_for(bench)
    job = service.jobs[matter.matter_id]
    source = store.source_changes.read(generated)[0]
    for complete in (True, False):
        done = set(service.production.discovery_done)
        if not complete:
            done.discard(matter.matter_id)
        monkeypatch.setattr(service.production, 'discovery_done', done)
        expected = service.production.document_projection(matter.matter_id, source.document_id, source.version_id)
        with service.repository.transaction(Budget(256, 224), 2 * 1024 * 1024) as tx:
            assert service.unassociated_projection(job, source, tx) == expected
    uploaded = next(source for source in store.source_changes.seek() if source.origin == 'upload')
    with service.repository.transaction(Budget(256, 224), 2 * 1024 * 1024) as tx:
        assert service.unassociated_projection(job, uploaded, tx) is None


def test_order_selects_a_durable_continuation_with_two_rows_remaining():
    budget = Budget(256, 224, work=222)
    source = dict(document_id='synthetic-document', version_id='synthetic-version',
                  display_name='record.txt', relative_path='record.txt', digest='', size=1,
                  media_type='text/plain', origin='registered', state='queued', has_text=False,
                  page_count=0, message='Queued for processing')
    row = (json.dumps(source), '', json.dumps({'display': asdict(sample_item(document='synthetic-document'))}), '')

    class SelectionRepository:
        def documents(self, tx, *args):
            tx.budget.charge(1)
            return [('synthetic-document',)]

        def document(self, tx, *args):
            tx.budget.charge(1)
            return row

        def clear_values(self, tx, *args):
            raise AssertionError('document selection must return before clearing values')

    cursor = {'active': None, 'after_document': '', 'generation': 'synthetic-generation'}
    prepared = {}
    service = SimpleNamespace(repository=SelectionRepository())
    job = SimpleNamespace(matter_id='synthetic-matter', definition=fields.load_definition(),
                          definition_digest='synthetic-definition')
    Service.order(service, SimpleNamespace(budget=budget), job, cursor, {'headers': []}, prepared)
    assert budget.work == 224
    assert cursor['active']['document_id'] == 'synthetic-document'
    assert cursor['active']['subphase'] == 'values'
    assert prepared['order_row'] == ('synthetic-document', row)


def test_order_finishes_a_full_value_batch_in_a_separate_primitive():
    definition = fields.load_definition()
    item = sample_item()
    stored, finished = [], []
    repo = SimpleNamespace(values=lambda tx, matter, item, selections, **kwargs:
                           stored.extend(chosen.key for chosen, descending, components in selections))
    service = SimpleNamespace(repository=repo, finish_order=lambda *args: finished.append(args))
    cursor = {'active': {'document_id': item.document_id, 'value_eof': True,
                         'value_position': 2 * (1 + len(definition.fields)) - 1}}
    prepared = {'order_document': (item.document_id, {}, item, None, 'synthetic-fingerprint')}
    tx = SimpleNamespace(budget=Budget(256, 224))
    job = SimpleNamespace(matter_id='synthetic-matter', definition=definition)
    Service.order(service, tx, job, cursor, {'headers': []}, prepared)
    assert len(stored) == 1 and finished == []
    assert cursor['active']['value_position'] == 2 * (1 + len(definition.fields))
    Service.order(service, tx, job, cursor, {'headers': []}, prepared)
    assert len(stored) == 1 and len(finished) == 1


def test_order_checkpoints_always_retain_an_active_document(production, monkeypatch):
    client, bench, matter = production
    add_population(bench.source_store(matter), 96)
    checkpoints = []
    save = Repository.save

    def observed_save(repository, tx, matter_id, cursor, summaries, exists):
        save(repository, tx, matter_id, cursor, summaries, exists)
        persisted = json.loads(repository.state(matter_id)['checkpoint'])
        if persisted['phase'] == 'order':
            checkpoints.append(persisted['active'])
            active = persisted['active']
            value_count = 2 * (1 + len(fields.load_definition().fields) + len(summaries['headers']))
            if active is not None and active['value_eof'] and active['value_position'] == value_count:
                assert tx.budget.remaining < 4

    monkeypatch.setattr(Repository, 'save', observed_save)
    drive(production, manifest(100))
    service_for(bench).invalidate(matter.matter_id)
    drive(production, manifest(100))
    assert checkpoints and all(checkpoint is not None for checkpoint in checkpoints)


@pytest.mark.parametrize('prior', [0, 1000, 1_000_000])
@pytest.mark.parametrize('interrupt', [False, True])
def test_checkpoint_window_restores_policy_and_full_durability(production, prior, interrupt):
    from contextlib import nullcontext
    client, bench, matter = production
    repo = service_for(bench).repository
    original = repo.rows('PRAGMA wal_autocheckpoint')[0][0]
    synchronous = repo.rows('PRAGMA synchronous')[0][0]
    page_size = repo.rows('PRAGMA page_size')[0][0]
    byte_limit = 8 * 1024 * 1024
    expected = byte_limit // (page_size + 24) if prior == 1000 else prior
    budget = Budget(256, 224)
    repo.rows(f'PRAGMA wal_autocheckpoint={prior}')
    try:
        failure = pytest.raises(RuntimeError, match='synthetic interrupted step') if interrupt else nullcontext()
        with failure:
            with repo.transaction(budget, 2 * 1024 * 1024, byte_limit):
                assert repo.checkpoint_metadata(budget, 'PRAGMA wal_autocheckpoint')[0][0] == expected
                assert repo.checkpoint_metadata(budget, 'PRAGMA synchronous')[0][0] == synchronous == 2
                if interrupt:
                    raise RuntimeError('synthetic interrupted step')
        assert repo.rows('PRAGMA wal_autocheckpoint')[0][0] == prior
        assert repo.rows('PRAGMA synchronous')[0][0] == synchronous
        assert budget.work == 0
        assert budget.control == budget.touches == (7 if prior == 1000 else 4 if prior == 0 else 5)
    finally:
        repo.rows(f'PRAGMA wal_autocheckpoint={original}')


def test_checkpoint_window_restores_policy_on_capacity_retry(production):
    from case_intelligence.modules.discovery_review.stored_index.models import CapacityPlan
    client, bench, matter = production
    repo = service_for(bench).repository
    original = repo.rows('PRAGMA wal_autocheckpoint')[0][0]
    budget = Budget(256, 224)
    with pytest.raises(CapacityPlan):
        with repo.transaction(budget, 2 * 1024 * 1024, 8 * 1024 * 1024):
            raise CapacityPlan(4 * 1024 * 1024)
    assert repo.rows('PRAGMA wal_autocheckpoint')[0][0] == original
    assert budget.control == budget.touches == 5 and budget.work == 0


def test_checkpoint_window_reserves_restore_control_before_commit(production):
    client, bench, matter = production
    repo = service_for(bench).repository
    original = repo.rows('PRAGMA wal_autocheckpoint')[0][0]
    budget = Budget(256, 224, control=28, touches=28)
    reached = []
    with pytest.raises(RuntimeError, match='index control row budget exceeded'):
        with repo.transaction(budget, 2 * 1024 * 1024, 8 * 1024 * 1024):
            reached.append(True)
    assert not reached
    assert repo.rows('PRAGMA wal_autocheckpoint')[0][0] == original


@pytest.mark.parametrize('ordering,headroom', [(False, False), (True, False), (True, True)])
def test_checkpoint_headroom_is_optional_and_first_step_keeps_original_capacity(ordering, headroom):
    from threading import Lock
    from case_intelligence.modules.discovery_review.stored_index.models import Job
    from case_intelligence.modules.discovery_review.stored_index.rebuild import MIN_CAPACITY, ORDER_CHECKPOINT_BYTES
    reservations, steps = [], []
    job = Job('synthetic-matter', None, None, 'synthetic-definition', order_checkpoint_window=ordering)

    def ensure_capacity(size):
        reservations.append(size)
        if size > MIN_CAPACITY and not headroom:
            raise RuntimeError('synthetic optional headroom unavailable')

    def step(job, budget, capacity, raw, plan):
        steps.append((capacity, job.reserved_checkpoint_bytes))
        return True

    service = SimpleNamespace(production=None, jobs_lock=Lock(), step_lock=Lock(),
                              repository=SimpleNamespace(sqlite=True),
                              context=SimpleNamespace(ensure_capacity=ensure_capacity), step=step)
    assert Service.advance(service, job, 1) == 1
    expected_window = ORDER_CHECKPOINT_BYTES if ordering and headroom else 0
    expected_reservations = [MIN_CAPACITY + 2 * ORDER_CHECKPOINT_BYTES] if ordering else []
    if not expected_window:
        expected_reservations.append(MIN_CAPACITY)
    assert reservations == expected_reservations
    assert steps == [(MIN_CAPACITY, expected_window)]
    assert job.reserved_checkpoint_bytes == 0 and not job.error
