"""Synthetic regressions for useful rebuild steps and version-two key bytes."""
from dataclasses import replace
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
