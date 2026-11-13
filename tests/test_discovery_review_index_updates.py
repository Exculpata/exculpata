"""Synthetic freshness and incomplete-metadata controls."""
from types import SimpleNamespace

import pytest

from case_intelligence.modules.discovery_review import fields, presentation


@pytest.mark.parametrize("state,note", [('pending', 'Metadata pending'),
                                        ('error', 'Metadata unavailable: indexing error')])
def test_pending_metadata_is_never_classified_as_absence(state, note):
    definition = fields.load_definition()
    item = SimpleNamespace(metadata_state=state, kind='document', values={}, details={}, load_file={},
                           value=lambda key: '', has=lambda key: False)
    chosen = definition.field('lf:Synthetic header')
    cell = presentation.cell(definition, chosen, item)
    assert cell['state'] == state and cell['value'] == note
    assert presentation.heading(definition, item)['label'] == note
    choice = presentation.Choice(definition.default_columns, (*definition.default_item_fields, chosen.key), 'default')
    shaped = presentation.item_fields(definition, item, choice)
    assert not any(group['empty'] for group in (*shaped['primary'], *shaped['more']))
    assert shaped['picked'][0]['absent'] is False
    assert shaped['load_file_state'] == note
    assert item.values == {} and item.load_file == {}


from tests.test_discovery_review_list import production as production, add_files, token_for
from tests.test_discovery_review_index import add_population, drive, manifest, service_for


def pending_job(production, monkeypatch):
    from case_intelligence.modules.discovery_review.stored_index.rebuild import Service
    client, bench, matter = production
    service = service_for(bench)
    with monkeypatch.context() as patch:
        patch.setattr(Service, 'advance', lambda self, job, max_steps: 0)
        view = service.current(matter.matter_id, bench.source_store(matter), fields.load_definition())
        assert not view.complete
    return service, service.jobs[matter.matter_id]


def finish_steps(service, job, population, *, observe=None):
    import json
    from case_intelligence.modules.discovery_review.stored_index.rebuild import work_bound
    _, _, limit = work_bound(**manifest(population))
    previous = -1
    for _ in range(limit):
        service.advance(job, 1)
        assert not job.error, getattr(service, 'failure', '')
        state = service.repository.state(job.matter_id)
        cursor = json.loads(state['checkpoint'])
        assert cursor['sequence'] > previous
        assert job.last_budget.touches <= 256
        if observe:
            observe(cursor)
        if state['phase'] == 'complete':
            return state
        previous = cursor['sequence']
    pytest.fail('bounded service driver exhausted manifest')


def test_one_source_change_reprojects_only_dependencies(production, monkeypatch):
    client, bench, matter = production
    drive(production, manifest(4))
    store = bench.source_store(matter)
    read, paths = store.source_path, []
    def source_path(document_id, **kwargs):
        paths.append(document_id)
        return read(document_id, **kwargs)
    monkeypatch.setattr(store, 'source_path', source_path)
    add_population(store, 1)
    drive(production, manifest(5))
    assert paths == []
    service = service_for(bench)
    before = service.repository.rows('SELECT document_id,projection_fingerprint FROM workbench_discovery_index_item WHERE matter_id=?', (matter.matter_id,))
    service.invalidate(matter.matter_id)
    drive(production, manifest(5))
    assert service.repository.rows('SELECT document_id,projection_fingerprint FROM workbench_discovery_index_item WHERE matter_id=?', (matter.matter_id,)) == before
    assert paths == []
    document = store.source_changes.lookup(token_for(bench, matter, 'memo.txt'))
    version = document.version_id
    store.mark_failed(document.document_id, 'Synthetic metadata update')
    drive(production, manifest(5))
    updated = store.source_changes.read([document.document_id])[0]
    assert updated.version_id == version and updated.state == 'failed'
    after = service.repository.rows('SELECT source_payload,projection_fingerprint FROM workbench_discovery_index_item WHERE matter_id=? AND document_id=?',
                                    (matter.matter_id, document.document_id))[0]
    assert 'Synthetic metadata update' in after[0]
    assert after[1] != dict(before)[document.document_id]
    assert paths == []


def test_match_ambiguity_and_precedence_are_recomputed(production):
    client, bench, matter = production
    drive(production, manifest(4))
    add_files(client, bench, matter, {'Earlier/a.csv': b'BEGBATES,NATIVEPATH\nEARLY2,VOL001/NATIVES/ledger.txt\n'}, key='b')
    page = drive(production, {**manifest(5), 'F': 2, 'L': 4, 'A': 60})
    assert 'EARLY2' in page.text
    store = bench.source_store(matter)
    document = next(d for d in store.documents.values() if d.display_name.endswith('a.csv'))
    store.remove(document.document_id)
    page = drive(production, manifest(4))
    assert 'EARLY2' not in page.text and 'SYN000010' in page.text


def test_restart_and_failed_notification_reconcile(production, monkeypatch):
    from case_intelligence.modules.discovery_review.stored_index.rebuild import Service
    client, bench, matter = production
    drive(production, manifest(4))
    store, original = bench.source_store(matter), service_for(bench)
    def broken(change):
        raise RuntimeError('synthetic notification failure')
    key, epoch, revision = store.source_changes.subscribe(broken)
    add_population(store, 1)
    assert store.source_changes.failed_revision > revision
    store.source_changes.unsubscribe(key)
    assert drive(production, manifest(5)).status_code == 200
    restored = Service(original.context, original.identifiers, original.codes)
    view = restored.current(matter.matter_id, store, fields.load_definition())
    assert view.complete and view.total == 5
    assert store.source_changes.epoch == epoch


def test_human_work_changes_refresh_derived_values(production):
    from tests.test_intake_receipt_http import ACTOR
    client, bench, matter = production
    drive(production, manifest(4))
    service = service_for(bench)
    store = bench.source_store(matter)
    document = store.source_changes.lookup(token_for(bench, matter, 'memo.txt'))
    code = service.codes.define(matter.matter_id, ACTOR, 'Synthetic tag')
    service.codes.record(matter.matter_id, document.document_id, code.code_id, ACTOR, 'apply')
    service.identifiers.assign(matter.matter_id, ACTOR, [(document.document_id, 'SYN100')])
    drive(production, {**manifest(4), 'C': 1, 'I': 1})
    from case_intelligence.modules.discovery_review.stored_index.query import lookup
    view = service.current(matter.matter_id, store, fields.load_definition())
    item = lookup(view, token_for(bench, matter, 'memo.txt'))
    assert item.value('assigned_id') == 'SYN100'
    assert 'Synthetic tag' in str(item.values)
    service.codes.record(matter.matter_id, document.document_id, code.code_id, ACTOR, 'remove')
    view = service.current(matter.matter_id, store, fields.load_definition())
    assert 'Synthetic tag' not in str(lookup(view, item.token).values)
    service.codes.record(matter.matter_id, document.document_id, code.code_id, ACTOR, 'apply')
    service.codes.retire(matter.matter_id, code.code_id)
    view = service.current(matter.matter_id, store, fields.load_definition())
    assert '(retired)' in str(lookup(view, item.token).values)


def test_pending_fallback_lists_every_registry_item(production, monkeypatch):
    from case_intelligence.modules.discovery_review.stored_index.rebuild import Service
    from tests.test_discovery_review_index import tokens, page_link
    client, bench, matter = production
    add_population(bench.source_store(matter), 297)
    monkeypatch.setattr(Service, 'advance', lambda self, job, max_steps: 0)
    page = client.get(f'/matters/{matter.slug}/production?q=never-match&sort=name&dir=desc')
    found = []
    while True:
        assert 'Showing all items in source order while indexing' in page.text
        assert 'Metadata pending' in page.text
        found.extend(tokens(page.text))
        link = page_link(page.text, 'next')
        if not link:
            break
        page = client.get(link)
    expected = {bench.source_store(matter).action_token(d) for d in bench.source_store(matter).documents.values()}
    assert len(found) == len(set(found)) == 301 and set(found) == expected


@pytest.mark.parametrize('discard', ['step', 'request'])
def test_cursor_resumes_after_step(production, monkeypatch, discard):
    import json
    from case_intelligence.modules.discovery_review.stored_index.rebuild import Service, work_bound
    from tests.test_intake_receipt_http import ACTOR
    client, bench, matter = production
    store = bench.source_store(matter)
    add_population(store, 397)
    content = ('BEGBATES,NATIVEPATH,Review Tag\n' +
               ''.join(f'RESUME{number},VOL001/NATIVES/ledger.txt,Value {number}\n' for number in range(300))).encode()
    add_files(client, bench, matter, {'Resume/rows.csv': content}, key='b')
    service, job = pending_job(production, monkeypatch)
    document = next(iter(store.documents))
    for number in range(200):
        code = service.codes.define(job.matter_id, ACTOR, f'Resume {number:03}')
        service.codes.record(job.matter_id, document, code.code_id, ACTOR, 'apply')
    dimensions = {**manifest(402), 'F': 2, 'L': 303, 'C': 200, 'P': 128, 'A': 303 * 402}
    _, _, limit = work_bound(**dimensions)
    seen, interior, sequence = set(), set(), -1
    batch = 1 if discard == 'step' else 128
    reconstructed, rolled_back = 0, False
    for _ in range((limit + batch - 1) // batch):
        service.advance(job, batch)
        assert not job.error, getattr(service, 'failure', '')
        state = service.repository.state(job.matter_id)
        cursor = json.loads(state['checkpoint'])
        assert cursor['sequence'] > sequence
        sequence = cursor['sequence']
        seen.add(cursor['phase'])
        if cursor['active']:
            interior.add(cursor['active']['subphase'])
        assert job.last_budget.touches <= 256
        if state['phase'] == 'complete':
            break
        if not rolled_back and cursor['active']:
            before_rows = service.repository.rows('SELECT document_id,payload,cache_payload FROM workbench_discovery_index_item WHERE matter_id=? ORDER BY document_id', (job.matter_id,))
            save = service.repository.save
            def abort_after_save(*args, **kwargs):
                save(*args, **kwargs)
                raise RuntimeError('synthetic interrupted commit')
            with monkeypatch.context() as patch:
                patch.setattr(service.repository, 'save', abort_after_save)
                service.advance(job, 1)
            assert job.error and service.repository.state(job.matter_id) == state
            assert service.repository.rows('SELECT document_id,payload,cache_payload FROM workbench_discovery_index_item WHERE matter_id=? ORDER BY document_id', (job.matter_id,)) == before_rows
            rolled_back = True
        context, identifiers, codes = service.context, service.identifiers, service.codes
        service.cancel(job.matter_id)
        service = Service(context, identifiers, codes)
        with monkeypatch.context() as patch:
            patch.setattr(Service, 'advance', lambda self, job, max_steps: 0)
            view = service.current(matter.matter_id, store, fields.load_definition())
            assert not view.complete
        job = service.jobs[matter.matter_id]
        reconstructed += 1
        assert service.repository.state(job.matter_id)['checkpoint'] == state['checkpoint']
    else:
        pytest.fail('persisted resume exhausted its generated manifest')
    assert reconstructed and rolled_back
    assert state['indexed'] == 402 and not state['errors']
    snapshot = service.repository.rows('SELECT document_id,payload FROM workbench_discovery_index_item WHERE matter_id=? ORDER BY document_id', (job.matter_id,))
    if discard == 'step':
        assert {'inventory', 'parse', 'match', 'reports', 'project', 'complete'} <= seen
        assert {'parse', 'match', 'report', 'coding', 'values'} <= interior
    service.invalidate(job.matter_id)
    for _ in range((limit + 127) // 128):
        service.advance(job, 128)
        state_again = service.repository.state(job.matter_id)
        assert not job.error
        if state_again['phase'] == 'complete':
            break
    assert state_again['phase'] == 'complete' and state_again['indexed'] == 402
    assert service.repository.rows('SELECT document_id,payload FROM workbench_discovery_index_item WHERE matter_id=? ORDER BY document_id', (job.matter_id,)) == snapshot


def test_cursor_resumes_after_request(production, monkeypatch):
    test_cursor_resumes_after_step(production, monkeypatch, 'request')


def test_cursor_restart_reconciles_new_epoch(production, monkeypatch):
    from case_intelligence.pilot_source_changes import SourceChanges
    service, job = pending_job(production, monkeypatch)
    service.advance(job, 1)
    generation = service.repository.state(job.matter_id)['generation']
    job.store.source_changes = SourceChanges(job.store)
    state = finish_steps(service, job, 4)
    assert state['generation'] != generation and state['indexed'] == 4


def test_cursor_generation_change_restarts_without_omission(production, monkeypatch):
    service, job = pending_job(production, monkeypatch)
    service.advance(job, 1)
    generation = service.repository.state(job.matter_id)['generation']
    add_population(job.store, 3)
    state = finish_steps(service, job, 7)
    assert state['generation'] != generation and state['indexed'] == 7
    found = service.repository.rows('SELECT document_id FROM workbench_discovery_index_item WHERE matter_id=?', (job.matter_id,))
    assert {row[0] for row in found} == set(job.store.documents)


def test_cursor_coding_invalidation_restarts_pages(production, monkeypatch):
    from tests.test_intake_receipt_http import ACTOR
    service, job = pending_job(production, monkeypatch)
    document = next(iter(job.store.documents))
    first = service.codes.define(job.matter_id, ACTOR, 'First')
    for number in range(20):
        code = service.codes.define(job.matter_id, ACTOR, f'Synthetic {number}')
        service.codes.record(job.matter_id, document, code.code_id, ACTOR, 'apply')
    service.advance(job, 1)
    service.codes.record(job.matter_id, document, first.code_id, ACTOR, 'apply')
    service.invalidate(job.matter_id)
    finish_steps(service, job, 4)
    row = service.repository.read_items(job.matter_id, [document])[document]
    assert 'First' in row[0] and 'Synthetic 19' in row[0]
