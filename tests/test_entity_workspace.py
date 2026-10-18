
from tests.fixture_write import fixture_write

from case_intelligence.legacy_names import (
    LEGACY_STEM,
)
from tests.case_lifecycle_web_helpers import claim_bench_exit
"""Synthetic manual identity, provenance, concurrency, lifecycle and web journeys."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
import json
import html
import re
from urllib.parse import parse_qs, urlparse
from pathlib import Path
import sqlite3
import threading

from fastapi.testclient import TestClient
import pytest

from case_intelligence.entity_repository import EntityEditConflict
from case_intelligence.entity_service import EntityService, REFERENCE_FIELDS, current_reference_indexes
from case_intelligence.workbench import create_workbench_app
from case_intelligence.workspace_store import WorkspaceProblem, WorkspaceStore
from tests.test_matter_notebook import ACTOR, WEB_ACTOR, _seed, _reference, _create_matter


def service(store, references=None):
    refs = references if references is not None else {'a' * 40: _reference(), 'b' * 40: _reference('b')}
    return EntityService(store.entity_repository(), source_guard=nullcontext,
                         resolve_support=lambda token: refs[token],
                         load_note=store.notebook_item, load_references=store.notebook_references,
                         validate_references=lambda values: frozenset(index for index, value in enumerate(values)
                             if value['support_token'] in refs and all(refs[value['support_token']][key] == value[key] for key in REFERENCE_FIELDS)))


def setup(tmp_path):
    store = WorkspaceStore(tmp_path / 'workspace.sqlite')
    _seed(store)
    matter = store.create_matter('Synthetic entities', '', ACTOR)
    return store, matter, service(store)


def test_manual_distinct_identities_mentions_aliases_and_correction_history(tmp_path):
    store, matter, entities = setup(tmp_path)
    first = entities.create(matter.matter_id, ACTOR, display_name='Alex Example', aliases='A. Example')
    other = entities.create(matter.matter_id, ACTOR, display_name='Alex Example', aliases='A. Example')
    assert first['entity_id'] != other['entity_id']
    first = entities.attach(matter.matter_id, ACTOR, first['entity_id'], expected_revision=1, support='a' * 40)
    first = entities.attach(matter.matter_id, ACTOR, first['entity_id'], expected_revision=2, support='b' * 40)
    repeated = entities.attach(matter.matter_id, ACTOR, first['entity_id'], expected_revision=3, support='a' * 40)
    assert repeated == first
    detail, mentions, history, _ = entities.detail(matter.matter_id, ACTOR, first['entity_id'])
    assert len(mentions) == 2 and all(item['available'] for item in mentions)
    assert [entry['revision'] for entry in history] == [3, 2, 1]
    assert entities.list(matter.matter_id, ACTOR, query='A. Example')[1] == 2
    entities.remove_mention(matter.matter_id, ACTOR, first['entity_id'], expected_revision=3, mention_id=mentions[0]['mention_id'])
    assert len(entities.detail(matter.matter_id, ACTOR, first['entity_id'])[1]) == 1
    assert len(json.loads(entities.detail(matter.matter_id, ACTOR, first['entity_id'])[2][0]['snapshot_json'])['removed_mentions']) == 1
    assert not entities.detail(matter.matter_id, ACTOR, other['entity_id'])[1]
    store.close()


def test_import_is_atomic_non_destructive_and_retains_review_provenance(tmp_path):
    store, matter, entities = setup(tmp_path)
    note, _ = store.create_notebook_item(matter.matter_id, ACTOR, item_type='person', status='suggested',
        title='Alex Example', body='Synthetic machine suggestion', origin='extraction', references=(_reference(), _reference('b')))
    before = store.notebook_references(matter.matter_id, ACTOR, note.item_id)
    imported = entities.import_note(matter.matter_id, ACTOR, note.item_id)
    assert imported['origin'] == 'notebook' and imported['status'] == 'suggested'
    assert imported['notebook_snapshot']['origin'] == 'extraction'
    assert store.notebook_item(matter.matter_id, ACTOR, note.item_id) == note
    assert store.notebook_references(matter.matter_id, ACTOR, note.item_id) == before
    entities.update(matter.matter_id, ACTOR, imported['entity_id'], expected_revision=1, display_name='Human correction', status='confirmed')
    assert entities.import_note(matter.matter_id, ACTOR, note.item_id)['display_name'] == 'Human correction'
    changed = store.update_notebook_item(matter.matter_id, ACTOR, note.item_id, expected_updated_at=note.updated_at,
        item_type='person', status='disputed', title='Corrected note', body='Synthetic commentary correction')
    detail = entities.detail(matter.matter_id, ACTOR, imported['entity_id'])
    assert detail[3] == changed and detail[0]['notebook_snapshot']['body'] == note.body
    assert len(detail[1]) == 2
    store.close()


def test_stale_source_import_rolls_back_and_existing_mentions_remain_historical(tmp_path):
    store, matter, entities = setup(tmp_path)
    note, _ = store.create_notebook_item(matter.matter_id, ACTOR, item_type='person', status='suggested',
        title='Synthetic unsupported import', origin='extraction', references=(_reference(), _reference('b')))
    refs = {'a' * 40: _reference()}
    stale = service(store, refs)
    with pytest.raises(WorkspaceProblem):
        stale.import_note(matter.matter_id, ACTOR, note.item_id)
    assert entities.list(matter.matter_id, ACTOR)[1] == 0
    first = entities.create(matter.matter_id, ACTOR, display_name='Synthetic identity', support='a' * 40)
    refs['a' * 40] = dict(_reference(), excerpt='Changed original without changing locator')
    assert not stale.detail(matter.matter_id, ACTOR, first['entity_id'])[1][0]['available']
    with pytest.raises(WorkspaceProblem, match='changed'):
        stale.import_note(matter.matter_id, ACTOR, note.item_id)
    assert entities.list(matter.matter_id, ACTOR)[1] == 1
    store.close()


def test_authority_cross_matter_and_two_connection_stale_writes(tmp_path):
    store, matter, entities = setup(tmp_path)
    first = entities.create(matter.matter_id, ACTOR, display_name='Synthetic identity')
    other = store.create_matter('Other synthetic matter', '', ACTOR)
    with pytest.raises(KeyError):
        entities.detail(other.matter_id, ACTOR, first['entity_id'])
    with pytest.raises(KeyError):
        entities.update(other.matter_id, ACTOR, first['entity_id'], expected_revision=1, display_name='Denied')
    member = 'synthetic-entity-member'
    _seed(store, member)
    store.add_member(matter.matter_id, member, ACTOR)
    second = WorkspaceStore(store.path)
    barrier = threading.Barrier(2)
    def change(svc, name, actor):
        barrier.wait(timeout=5)
        try:
            svc.update(matter.matter_id, actor, first['entity_id'], expected_revision=1, display_name=name)
            return 'saved'
        except EntityEditConflict:
            return 'conflict'
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(change, svc, label, actor) for svc, label, actor in ((entities, 'First', ACTOR), (service(second), 'Second', member))]
        assert sorted(f.result(timeout=10) for f in futures) == ['conflict', 'saved']
    with fixture_write(store):
        store.connection.execute('UPDATE workbench_principal SET active=0 WHERE principal_id=?', (ACTOR,))
    with pytest.raises(KeyError):
        service(second).create(matter.matter_id, ACTOR, display_name='Denied')
    with pytest.raises(KeyError):
        entities.detail(matter.matter_id, ACTOR, first['entity_id'])
    second.close()
    store.close()


def test_mirror_backup_clean_restore_and_entity_deletion(tmp_path):
    root = Path(__file__).parents[1]
    migration = 'migrations/sqlite/0032_entity_workspace.sql'
    assert (root / migration).read_bytes() == (root / 'src/case_intelligence' / migration).read_bytes()
    store, matter, entities = setup(tmp_path)
    entity = entities.create(matter.matter_id, ACTOR, display_name='Synthetic restored identity', support='a' * 40)
    original = entities.detail(matter.matter_id, ACTOR, entity['entity_id'])
    backup = tmp_path / 'backup.sqlite'
    with sqlite3.connect(backup) as target:
        with store._lock:
            store.connection.backup(target)
    restore = tmp_path / 'clean' / 'workspace.sqlite'
    restore.parent.mkdir()
    with sqlite3.connect(backup) as source, sqlite3.connect(restore) as target:
        source.backup(target)
    restored = WorkspaceStore(restore)
    assert service(restored).detail(matter.matter_id, ACTOR, entity['entity_id']) == original
    assert restored.connection.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    assert not restored.connection.execute('PRAGMA foreign_key_check').fetchall()
    service(restored).delete(matter.matter_id, ACTOR, entity['entity_id'], expected_revision=1)
    for table in ('entity', 'entity_mention', 'entity_history'):
        assert restored.connection.execute('SELECT COUNT(*) FROM workbench_' + table).fetchone()[0] == 0
    restored.close()
    store.close()


def test_web_journey_original_passages_context_same_name_and_edit_recovery(tmp_path, monkeypatch):
    monkeypatch.setenv('CASE_INTELLIGENCE_STORAGE_RESERVE_GIB', '0')
    with TestClient(create_workbench_app(tmp_path / 'runtime', auth_mode='test')) as client:
        slug = _create_matter(client, 'Synthetic identity browser journey')
        path = f'/matters/{slug}/entities'
        assert 'No entities yet' in client.get(path).text
        for index, text in enumerate(('Alex Example visited the synthetic depot.', 'Alex Example called the synthetic archive.')):
            response = client.post(f'/matters/{slug}/uploads', files=[('files', (f'Generated-{index}.txt', text.encode(), 'text/plain'))])
            assert response.status_code == 200
        bench = client.app.state.workbench
        matter = bench.matter(slug, WEB_ACTOR)
        store = bench.source_store(matter)
        tokens = [bench._citation(matter, bench._candidate(matter, document, document.parsed_units()[0], 1)).support_token for document in store.documents.values()]
        assert len(tokens) == 2
        search_path = f'/matters/{slug}?mode=search&q=synthetic'
        search = client.get(search_path)
        support_href = html.unescape(re.search(r'class="open-support-link" href="([^"]+)"', search.text).group(1))
        support_page = client.get(support_href)
        entity_href = html.unescape(re.search(r'href="([^"]+/entities\?support=[^"]+)"', support_page.text).group(1))
        assert parse_qs(urlparse(entity_href).query)['return_to'] == [search_path + '#search-results']
        full_href = html.unescape(re.search(r'class="support-header-open" href="([^"]+)"', support_page.text).group(1))
        full_page = client.get(full_href)
        full_entity_href = html.unescape(re.search(r'href="([^"]+/entities\?support=[^"]+)"', full_page.text).group(1))
        assert parse_qs(urlparse(full_entity_href).query)['return_to'] == [search_path + '#search-results']
        context = f'/matters/{slug}?mode=search&q=Alex&page=2#support-pane'
        response = client.post(path + '/actions', data=dict(action='create', display_name='Alex Example', aliases='A. Example', support=tokens[0], return_to=context), follow_redirects=False)
        assert response.status_code == 303
        detail_url = response.headers['location']
        first = bench.entity_service(matter).list(matter.matter_id, WEB_ACTOR)[0][0]
        response = client.post(path + '/actions', data=dict(action='attach', entity_id=first['entity_id'], expected_revision=1, support=tokens[1]))
        assert response.status_code == 200 and 'Original-source mentions (2)' in response.text
        for token in tokens:
            assert client.get(f'/matters/{slug}?support={token}').status_code == 200
        response = client.post(path + '/actions', data=dict(action='create', display_name='Alex Example'))
        assert response.status_code == 200
        assert bench.entity_service(matter).list(matter.matter_id, WEB_ACTOR)[1] == 2
        response = client.post(path + '/actions', data=dict(action='update', entity_id=first['entity_id'], expected_revision=1, display_name='Unsaved correction', aliases='Unsaved alias'))
        assert response.status_code == 409
        assert 'Unsaved correction' in response.text and 'Unsaved alias' in response.text and 'Alex Example' in response.text
        assert 'Return to source review' in client.get(detail_url).text
        exported = client.get(path + '/' + first['entity_id'] + '/export')
        assert exported.status_code == 200 and len(exported.json()['mentions']) == 2


def test_actual_sources_survive_stopped_backup_clean_restore_and_bundle(tmp_path, monkeypatch):
    import io
    import shutil
    import zipfile
    monkeypatch.setenv('CASE_INTELLIGENCE_STORAGE_RESERVE_GIB', '0')
    runtime = tmp_path / 'original-runtime'
    with TestClient(create_workbench_app(runtime, auth_mode='test')) as client:
        slug = _create_matter(client)
        client.post(f'/matters/{slug}/uploads', files=[('files', ('Generated original.txt', b'Alex Example visited a synthetic depot.', 'text/plain'))])
        bench = client.app.state.workbench
        matter = bench.matter(slug, WEB_ACTOR)
        document = next(iter(bench.source_store(matter).documents.values()))
        token = bench._support_token(bench._candidate(matter, document, document.parsed_units()[0], 1))
        entity = bench.entity_service(matter).create(matter.matter_id, WEB_ACTOR, display_name='Alex Example', support=token)
        expected = bench.entity_service(matter).detail(matter.matter_id, WEB_ACTOR, entity['entity_id'])
        source_page = client.get(f'/matters/{slug}/sources/{bench.source_store(matter).action_token(document)}')
        assert source_page.status_code == 200 and 'Attach passage to entity' in source_page.text
        other_slug = _create_matter(client, 'Other synthetic scope')
        response = client.post(f'/matters/{other_slug}/entities/actions', data=dict(action='create', display_name='Denied copy', support=token))
        assert response.status_code == 409
        assert bench.entity_service(bench.matter(other_slug, WEB_ACTOR)).list(bench.matter(other_slug, WEB_ACTOR).matter_id, WEB_ACTOR)[1] == 0
        bundle = client.get(f'/matters/{slug}/export')
        assert bundle.status_code == 200
        with zipfile.ZipFile(io.BytesIO(bundle.content)) as archive:
            exported = json.loads(archive.read('entities/001-entity.json'))
        assert exported['entity']['entity_id'] == entity['entity_id']
        assert exported['mentions'][0]['excerpt'] == expected[1][0]['excerpt']
        assert exported['history']
    # Writers stopped: copy the complete control/session/source boundary twice,
    # first into a backup and then into a previously absent clean target.
    backup = tmp_path / 'stopped-backup'
    restored = tmp_path / 'restored-runtime'
    shutil.copytree(runtime, backup)
    shutil.copytree(backup, restored)
    shutil.move(runtime, tmp_path / 'original-runtime-offline')
    shutil.move(backup, tmp_path / 'backup-offline')
    with TestClient(create_workbench_app(restored, auth_mode='test')) as client:
        bench = client.app.state.workbench
        matter = bench.matter(slug, WEB_ACTOR)
        actual = bench.entity_service(matter).detail(matter.matter_id, WEB_ACTOR, entity['entity_id'])
        assert actual == expected and actual[1][0]['available']
        response = client.get(f'/matters/{slug}?support={token}')
        assert response.status_code == 200 and 'visited a synthetic depot' in response.text
        restored_store = bench.source_store(matter)
        restored_document = next(iter(restored_store.documents.values()))
        original_content = client.get(f'/matters/{slug}/sources/{restored_store.action_token(restored_document)}/content')
        assert original_content.status_code == 200 and b'visited a synthetic depot' in original_content.content
        prepared, lifecycle = claim_bench_exit(bench, matter)
        bench.execute_matter_purge(prepared, lifecycle)
        for table in ('entity', 'entity_mention', 'entity_history'):
            assert bench.workspace.connection.execute('SELECT COUNT(*) FROM workbench_' + table + ' WHERE matter_id=?', (matter.matter_id,)).fetchone()[0] == 0


def test_entity_validation_and_full_pagination_without_identity_collapse(tmp_path):
    store, matter, entities = setup(tmp_path)
    for bad in ('', 'x' * 161, 'Synthetic\x00name'):
        with pytest.raises(WorkspaceProblem):
            entities.create(matter.matter_id, ACTOR, display_name=bad)
    for index in range(53):
        entities.create(matter.matter_id, ACTOR, display_name='Synthetic repeated name', aliases='Alias % label')
    first, count = entities.list(matter.matter_id, ACTOR)
    second, _ = entities.list(matter.matter_id, ACTOR, page=2)
    assert count == 53 and len(first) == 50 and len(second) == 3
    assert len({item['entity_id'] for item in first + second}) == 53
    assert entities.list(matter.matter_id, ACTOR, query='%')[1] == 53
    assert entities.list(matter.matter_id, ACTOR, query='_')[1] == 0
    assert entities.list(matter.matter_id, ACTOR, query='[')[1] == 0
    store.close()


def test_http_csrf_membership_and_source_version_fail_closed(tmp_path, monkeypatch):
    from tests.test_matter_management import OWNER, OTHER, _app, _csrf, _headers, _principal_id
    from tests.test_matter_management import _create_matter as create_authorized_matter
    monkeypatch.setenv('CASE_INTELLIGENCE_STORAGE_RESERVE_GIB', '0')
    with TestClient(_app(tmp_path), base_url=f"https://{LEGACY_STEM}.example.test") as client:
        page = client.get('/matters/new', headers=_headers(OWNER))
        csrf = _csrf(page.text)
        slug = create_authorized_matter(client, principal=OWNER, csrf_token=csrf, name='Synthetic protected entity')
        path = f'/matters/{slug}/entities'
        denied = client.post(path + '/actions', headers=_headers(OWNER), data=dict(action='create', display_name='Missing CSRF'))
        assert denied.status_code == 403
        saved = client.post(path + '/actions', headers=_headers(OWNER), data=dict(csrf_token=csrf, action='create', display_name='Synthetic private-to-matter identity'), follow_redirects=False)
        assert saved.status_code == 303
        assert client.get(path, headers=_headers(OTHER)).status_code == 404
        assert client.get(saved.headers['location'], headers=_headers(OTHER)).status_code == 404
        csrf = _csrf(client.get(path, headers=_headers(OWNER)).text)
        upload = client.post(f'/matters/{slug}/uploads', headers=_headers(OWNER), data=dict(csrf_token=csrf), files=[('files', ('Generated.txt', b'Alex Example at the synthetic depot.', 'text/plain'))])
        assert upload.status_code == 200
        bench = client.app.state.workbench
        actor = _principal_id(client, OWNER)
        matter = bench.matter(slug, actor)
        store = bench.source_store(matter)
        document = next(iter(store.documents.values()))
        token = bench._support_token(bench._candidate(matter, document, document.parsed_units()[0], 1))
        entity = bench.entity_service(matter).list(matter.matter_id, actor)[0][0]
        attached = client.post(path + '/actions', headers=_headers(OWNER), data=dict(csrf_token=csrf, action='attach', entity_id=entity['entity_id'], expected_revision=1, support=token))
        assert attached.status_code == 200
        with store.mutation_guard():
            document.version_id = 'f' * 32
            store._save()
        detail = client.get(path + '/' + entity['entity_id'], headers=_headers(OWNER))
        assert 'Original passage changed or is unavailable' in detail.text
        assert 'Open original passage' not in detail.text
        stale = client.post(path + '/actions', headers=_headers(OWNER), data=dict(csrf_token=csrf, action='create', display_name='Unsaved stale source identity', support=token))
        assert stale.status_code == 409 and 'Unsaved stale source identity' in stale.text
        assert bench.entity_service(matter).list(matter.matter_id, actor)[1] == 1


def test_mention_history_grows_linearly_and_retains_removed_originals(tmp_path):
    store, matter, _ = setup(tmp_path)
    refs = {}
    for index in range(40):
        token = f'{index:040x}'
        refs[token] = dict(_reference(), support_token=token,
                           excerpt=f'Synthetic mention {index}: ' + 'x' * 5900)
    entities = service(store, refs)
    entity = entities.create(matter.matter_id, ACTOR, display_name='Synthetic many-mention identity')
    for token in refs:
        entity = entities.attach(matter.matter_id, ACTOR, entity['entity_id'],
                                 expected_revision=entity['revision'], support=token)
    entity = entities.update(matter.matter_id, ACTOR, entity['entity_id'],
                             expected_revision=entity['revision'], display_name='Synthetic human correction')
    _, mentions, history, _ = entities.detail(matter.matter_id, ACTOR, entity['entity_id'])
    snapshots = [json.loads(entry['snapshot_json']) for entry in history]
    assert sum(len(entry['added_mentions']) for entry in snapshots) == 40
    assert all('mentions' not in entry for entry in snapshots)
    assert len(''.join(entry['snapshot_json'] for entry in history)) < 400_000
    removed = mentions[0]
    entities.remove_mention(matter.matter_id, ACTOR, entity['entity_id'],
                             expected_revision=entity['revision'], mention_id=removed['mention_id'])
    _, current_mentions, updated_history, _ = entities.detail(matter.matter_id, ACTOR, entity['entity_id'])
    change = json.loads(updated_history[0]['snapshot_json'])
    assert change['removed_mentions'][0]['excerpt'] == removed['excerpt']
    assert change['added_mentions'] == [] and len(current_mentions) == 39
    store.close()


def test_availability_parses_only_referenced_documents_once_and_checks_every_field():
    from types import SimpleNamespace
    references = [_reference(), _reference('b'), _reference(), dict(_reference(), excerpt='Tampered quote')]
    loads, parses, candidates = [], [], []
    canonical = {item['document_id']: item for item in references[:2]}
    def load(document_id):
        loads.append(document_id)
        reference = canonical[document_id]
        def units():
            parses.append(document_id)
            return [SimpleNamespace(number=1)]
        return SimpleNamespace(state='ready', document_id=document_id, parsed_units=units)
    def candidate_for(document, unit, ordinal):
        candidates.append(document.document_id)
        reference = canonical[document.document_id]
        return SimpleNamespace(document_id=document.document_id, source_version_id=reference['source_version_id'],
            source_name=reference['source_name'], citation=reference['location'], chunk_id='chunk-1',
            excerpt_digest=reference['excerpt_digest'], text=reference['excerpt'], token=reference['support_token'])
    available = current_reference_indexes(references, load_document=load, candidate_for=candidate_for,
                                          support_tokens=lambda value: (value.token,))
    assert available == frozenset({0, 1, 2})
    assert len(loads) == len(parses) == len(candidates) == 2


def test_deleted_identity_remains_identifiable_in_content_free_audit(tmp_path):
    with TestClient(create_workbench_app(tmp_path / 'runtime', auth_mode='test')) as client:
        slug = _create_matter(client)
        bench = client.app.state.workbench
        matter = bench.matter(slug, WEB_ACTOR)
        entity = bench.entity_service(matter).create(matter.matter_id, WEB_ACTOR, display_name='Synthetic deleted identity')
        response = client.post(f'/matters/{slug}/entities/actions', data=dict(action='delete',
            entity_id=entity['entity_id'], expected_revision=1), follow_redirects=False)
        assert response.status_code == 303 and '/entities?' in response.headers['location']
        event = next(item for item in bench.workspace.audit_events(matter.matter_id) if item.action == 'entity.delete')
        assert event.object_id == entity['entity_id'] and event.object_type == 'entity'
        assert 'Synthetic deleted identity' not in json.dumps(event.details)


def test_existing_full_history_snapshot_survives_new_delta_edits(tmp_path):
    store, matter, entities = setup(tmp_path)
    entity = entities.create(matter.matter_id, ACTOR, display_name='Synthetic earlier snapshot', support='a' * 40)
    history = entities.detail(matter.matter_id, ACTOR, entity['entity_id'])[2]
    legacy = json.loads(history[0]['snapshot_json'])
    legacy['mentions'] = legacy.pop('added_mentions')
    legacy.pop('removed_mentions')
    legacy.pop('history_format')
    legacy_json = json.dumps(legacy)
    with fixture_write(store):
        store.connection.execute('UPDATE workbench_entity_history SET snapshot_json=? WHERE entity_id=? AND revision=1',
                                  (legacy_json, entity['entity_id']))
    entities.update(matter.matter_id, ACTOR, entity['entity_id'], expected_revision=1, display_name='Synthetic corrected label')
    history = entities.detail(matter.matter_id, ACTOR, entity['entity_id'])[2]
    assert history[-1]['snapshot_json'] == legacy_json
    assert json.loads(history[0]['snapshot_json'])['history_format'] == 2
    assert json.loads(history[0]['snapshot_json'])['added_mentions'] == []
    store.close()
