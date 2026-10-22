"""Synthetic source-to-entity-to-assertion journeys, authority and complete recovery."""

from case_intelligence.legacy_names import (
    LEGACY_STEM,
)
from tests.case_lifecycle_web_helpers import claim_bench_exit
from case_intelligence.workspace_store import WorkspaceProblem
import html
import io
import json
import re
import shutil
import zipfile
from urllib.parse import parse_qs, urlencode, urlparse

from fastapi.testclient import TestClient
import pytest
from case_intelligence.generation import UnavailableGenerator
from case_intelligence.workbench import create_workbench_app
from tests.test_matter_notebook import WEB_ACTOR, _create_matter


ORIGINALS = {
    'Generated supporting account.txt': 'Morgan Sample says Alex Example delivered the red parcel to Cedar Depot on 03/04/2026.',
    'Generated competing account.txt': 'Riley Demo says Alex Example did not deliver the red parcel to Cedar Depot on 03/04/2026.',
    'Generated unrelated identity.txt': 'Alex Example, the unrelated museum volunteer, catalogued postcards.',
    'Generated visitor list.txt': 'The visitor list contains Jordan Sample and Casey Demo. No interaction or relationship is recorded.',
}
EVENT_FIELDS = dict(record_type='event', title='Disputed parcel delivery',
    statement='Alex Example delivered the red parcel to Cedar Depot.',
    raw_date='03/04/2026', date_uncertainty='Day/month order and time zone are unresolved.',
    sort_date='', status='needs_review')


def _app(runtime):
    return create_workbench_app(runtime, generator=UnavailableGenerator(), auth_mode='test')


def _upload(client, slug, *, headers=None, csrf=''):
    response = client.post(f'/matters/{slug}/uploads', headers=headers,
        data={'csrf_token': csrf}, files=[('files', (name, text.encode(), 'text/plain'))
            for name, text in ORIGINALS.items()])
    assert response.status_code == 200


def _passage(client, slug, term):
    path = f'/matters/{slug}?' + urlencode(dict(mode='search', q=term))
    response = client.get(path)
    assert response.status_code == 200
    match = re.search(r'class="open-support-link" href="([^"]+)"', response.text)
    assert match, response.text
    href = html.unescape(match.group(1))
    page = client.get(href)
    assert page.status_code == 200 and term in page.text
    token = parse_qs(urlparse(href).query)['support'][0]
    return token, path + '#search-results', page


def _entity(client, slug, name, *, support='', context='', entity_type='person'):
    response = client.post(f'/matters/{slug}/entities/actions', data=dict(
        action='create', display_name=name, support=support, return_to=context,
        entity_type=entity_type), follow_redirects=False)
    assert response.status_code == 303, response.text
    return urlparse(response.headers['location']).path.split('/')[-1]


def _create_event(client, slug, entity_id, token, *, context='', **fields):
    bench = client.app.state.workbench
    matter = bench.matter(slug, WEB_ACTOR)
    entity = bench.entity_service(matter).detail(matter.matter_id, WEB_ACTOR, entity_id)[0]
    response = client.post(f'/matters/{slug}/assertions/actions', data=dict(
        action='create', entity_id=entity_id, entity_revision=entity['revision'], role='subject',
        support=token, attributed_to='Morgan Sample, supporting account', return_to=context,
        **(EVENT_FIELDS | fields)), follow_redirects=False)
    assert response.status_code == 303, response.text
    return urlparse(response.headers['location']).path.split('/')[-1]


def _detail(client, slug, assertion_id):
    bench = client.app.state.workbench
    matter = bench.matter(slug, WEB_ACTOR)
    return bench.assertion_service(matter).detail(matter.matter_id, WEB_ACTOR, assertion_id)


def _action(client, slug, assertion_id, action, *, revision=None, **values):
    if revision is None:
        revision = _detail(client, slug, assertion_id)['record']['revision']
    return client.post(f'/matters/{slug}/assertions/actions', data=dict(
        action=action, assertion_id=assertion_id, expected_revision=revision, **values))


def test_originals_to_entity_disputed_event_clean_restore_export_and_actual_purge(tmp_path, monkeypatch):
    monkeypatch.setenv('CASE_INTELLIGENCE_STORAGE_RESERVE_GIB', '0')
    runtime = tmp_path / 'original-runtime'
    with TestClient(_app(runtime)) as client:
        slug = _create_matter(client, 'Synthetic conflicting source accounts')
        prefix = f'/matters/{slug}'
        _upload(client, slug)
        token_a, context, source_page = _passage(client, slug, 'Morgan')
        entity_link = re.search(r'href="([^"]+/entities\?support=[^"]+)"', source_page.text)
        assert entity_link and parse_qs(urlparse(html.unescape(entity_link.group(1))).query)['return_to'] == [context]
        entity_id = _entity(client, slug, 'Alex Example', support=token_a, context=context)
        entity_page = client.get(prefix + '/entities/' + entity_id)
        assert 'Create event or assertion' in entity_page.text
        entry = client.get(prefix + '/assertions/new', params=dict(entity_id=entity_id, support=token_a, return_to=context))
        assert entry.status_code == 200 and ORIGINALS['Generated supporting account.txt'] in entry.text
        assertion_id = _create_event(client, slug, entity_id, token_a, context=context)
        token_b, _, _ = _passage(client, slug, 'Riley')
        assert _action(client, slug, assertion_id, 'attach', support=token_b,
            stance='competing', attributed_to='Riley Demo, competing account').status_code == 200
        depot_id = _entity(client, slug, 'Cedar Depot', entity_type='place', support=token_a)
        assert _action(client, slug, assertion_id, 'add_role',
            entity_selection=f'{depot_id}:1', role='location').status_code == 200
        detail = _detail(client, slug, assertion_id)
        assert detail['record']['raw_date'] == '03/04/2026'
        assert not detail['record']['sort_date']
        assert detail['record']['status'] == 'needs_review'
        assert {(a['stance'], a['attributed_to']) for a in detail['accounts']} == {
            ('supporting', 'Morgan Sample, supporting account'), ('competing', 'Riley Demo, competing account')}
        assert {a['excerpt'] for a in detail['accounts']} == set(list(ORIGINALS.values())[:2])
        assert all(a['available'] for a in detail['accounts'])
        assert {(r['entity_id'], r['role']) for r in detail['roles']} == {
            (entity_id, 'subject'), (depot_id, 'location')}
        for account in detail['accounts']:
            inspected = client.get(prefix, params=dict(support=account['support_token']))
            assert inspected.status_code == 200 and account['excerpt'] in inspected.text
        assert _action(client, slug, assertion_id, 'update', **(EVENT_FIELDS | dict(status='confirmed'))).status_code == 200
        corrected = EVENT_FIELDS | dict(status='disputed', title='Delivery accounts remain incompatible',
            statement='Morgan reports a delivery; Riley denies that delivery. Review has not resolved the disagreement.')
        assert _action(client, slug, assertion_id, 'update', **corrected).status_code == 200
        # A correction changes a reviewer interpretation, never either original account.
        assert _detail(client, slug, assertion_id)['accounts'] == detail['accounts']
        unrelated_token, _, _ = _passage(client, slug, 'museum')
        unrelated_id = _entity(client, slug, 'Alex Example', support=unrelated_token)
        co_token, _, _ = _passage(client, slug, 'visitor')
        co_ids = [_entity(client, slug, name, support=co_token) for name in ('Jordan Sample', 'Casey Demo')]
        assert unrelated_id != entity_id
        for other_id in (unrelated_id, *co_ids):
            unrelated_page = client.get(prefix + '/entities/' + other_id)
            assert corrected['title'] not in unrelated_page.text
        # A second explicit human assertion can be corrected and removed.
        removed_id = _create_event(client, slug, entity_id, token_a,
            record_type='assertion', title='Temporary reviewer interpretation')
        assert _action(client, slug, removed_id, 'update', **(EVENT_FIELDS | dict(
            record_type='assertion', title='Corrected temporary interpretation', status='disputed'))).status_code == 200
        assert _action(client, slug, removed_id, 'delete').status_code == 200
        assert client.get(prefix + '/assertions/' + removed_id).status_code == 404
        refresh = client.post(prefix + '/analysis/refresh')
        assert refresh.status_code == 200
        chronology = client.get(prefix + '/chronology')
        assert chronology.status_code == 200 and corrected['title'] in chronology.text
        assert 'Corrected temporary interpretation' not in chronology.text
        assert client.get(prefix + '/assertions/' + removed_id).status_code == 404
        returned = client.get(prefix + '/assertions/' + assertion_id, params=dict(return_to=context))
        assert 'Return to source review' in returned.text
        assert html.escape(context, quote=True) in returned.text
        expected = _detail(client, slug, assertion_id)
        assert expected['record']['status'] == 'disputed' and len(expected['history']) >= 4
        exported = client.get(prefix + '/assertions/' + assertion_id + '/export')
        assert exported.status_code == 200
        assert exported.json()['record'] == expected['record']
        assert exported.json()['accounts'] == expected['accounts']
        chronology_json = client.get(prefix + '/chronology/export', params=dict(format='json'))
        chronology_markdown = client.get(prefix + '/chronology/export', params=dict(format='markdown'))
        assert chronology_json.status_code == chronology_markdown.status_code == 200
        for content in (chronology_json.text, chronology_markdown.text):
            assert '03/04/2026' in content and 'Day/month order' in content
            assert 'Morgan Sample' in content and 'Riley Demo' in content
            assert 'disputed' in content.casefold() and 'competing' in content.casefold()
            assert removed_id not in content
        bundle = client.get(prefix + '/export')
        assert bundle.status_code == 200
        with zipfile.ZipFile(io.BytesIO(bundle.content)) as archive:
            assertion_files = [name for name in archive.namelist() if name.startswith('assertions/') and name.endswith('.json')]
            assert assertion_files
            bundled = '\n'.join(archive.read(name).decode() for name in assertion_files)
            assert assertion_id in bundled and 'Morgan Sample' in bundled and 'Riley Demo' in bundled
            assert 'competing' in bundled and '03/04/2026' in bundled
        bench = client.app.state.workbench
        matter = bench.matter(slug, WEB_ACTOR)
        originals = {document.document_id: (document.display_name, document.version_id)
            for document in bench.source_store(matter).documents.values()}
    # Stop all writers and copy the full control/session/source boundary. Neither
    # the old runtime nor the backup can satisfy a restored original-source read.
    backup, restored = tmp_path / 'stopped-backup', tmp_path / 'clean-restore'
    shutil.copytree(runtime, backup)
    shutil.copytree(backup, restored)
    shutil.move(runtime, tmp_path / 'original-offline')
    shutil.move(backup, tmp_path / 'backup-offline')
    with TestClient(_app(restored)) as client:
        assert _detail(client, slug, assertion_id) == expected
        assert client.get(prefix + '/assertions/' + assertion_id + '/export').json() == exported.json()
        bench = client.app.state.workbench
        matter = bench.matter(slug, WEB_ACTOR)
        store = bench.source_store(matter)
        for document_id, (name, version) in originals.items():
            document = store.get(document_id)
            assert document.version_id == version
            response = client.get(prefix + '/sources/' + store.action_token(document) + '/content')
            assert response.status_code == 200 and response.content == ORIGINALS[name].encode()
        for account in expected['accounts']:
            page = client.get(prefix, params=dict(support=account['support_token']))
            assert page.status_code == 200 and account['excerpt'] in page.text
        assert bench.workspace.connection.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        assert not bench.workspace.connection.execute('PRAGMA foreign_key_check').fetchall()
        source_root = bench.storage.matters / matter.matter_id
        assert source_root.exists()
        prepared, lifecycle = claim_bench_exit(bench, matter)
        bench.execute_matter_purge(prepared, lifecycle)
        assert bench.workspace.matter_lifecycle(matter.matter_id).state == 'deleted'
        assert not source_root.exists()
        assert client.get(prefix + '/assertions/' + assertion_id).status_code == 404
        tables = [r[0] for r in bench.workspace.connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'workbench_assertion%'")]
        assert tables
        for table in tables:
            assert bench.workspace.connection.execute('SELECT COUNT(*) FROM ' + table + ' WHERE matter_id=?', (matter.matter_id,)).fetchone()[0] == 0


def test_stale_http_record_and_entity_roles_preserve_submitted_work(tmp_path, monkeypatch):
    monkeypatch.setenv('CASE_INTELLIGENCE_STORAGE_RESERVE_GIB', '0')
    with TestClient(_app(tmp_path / 'runtime')) as client:
        slug = _create_matter(client)
        _upload(client, slug)
        token, _, _ = _passage(client, slug, 'Morgan')
        entity_id = _entity(client, slug, 'Alex Example', support=token)
        assertion_id = _create_event(client, slug, entity_id, token)
        assert _action(client, slug, assertion_id, 'update', **(EVENT_FIELDS | dict(status='confirmed'))).status_code == 200
        unsaved = EVENT_FIELDS | dict(title='Unsaved reviewer correction', statement='Unsaved interpretation to compare.',
            raw_date='around 03/04/2026', date_uncertainty='Unsaved date caveat', status='disputed')
        conflict = _action(client, slug, assertion_id, 'update', revision=1, **unsaved)
        assert conflict.status_code == 409
        for key in ('title', 'statement', 'raw_date', 'date_uncertainty'):
            assert unsaved[key] in conflict.text
        assert _detail(client, slug, assertion_id)['record']['status'] == 'confirmed'
        assert _action(client, slug, assertion_id, 'update', **unsaved).status_code == 200
        bench = client.app.state.workbench
        matter = bench.matter(slug, WEB_ACTOR)
        entity = bench.entity_service(matter).detail(matter.matter_id, WEB_ACTOR, entity_id)[0]
        bench.entity_service(matter).update(matter.matter_id, WEB_ACTOR, entity_id,
            expected_revision=entity['revision'], display_name='Alex Example, corrected identity')
        role_conflict = _action(client, slug, assertion_id, 'add_role',
            entity_selection=f"{entity_id}:{entity['revision']}", role='participant')
        assert role_conflict.status_code == 409
        roles = _detail(client, slug, assertion_id)['roles']
        assert len(roles) == 1 and roles[0]['identity_state'] == 'changed'
        # The ordinary role form can replace the only role atomically, without
        # requiring a temporary duplicate or briefly removing all identity roles.
        current_entity = bench.entity_service(matter).detail(matter.matter_id, WEB_ACTOR, entity_id)[0]
        corrected = _action(client, slug, assertion_id, 'add_role', role_id=roles[0]['role_id'],
            entity_selection=f"{entity_id}:{current_entity['revision']}", role='participant')
        assert corrected.status_code == 200
        final = _detail(client, slug, assertion_id)
        assert len(final['roles']) == 1
        assert final['roles'][0]['role_id'] == roles[0]['role_id']
        assert final['roles'][0]['role'] == 'participant'
        assert final['roles'][0]['display_name'] == 'Alex Example, corrected identity'
        assert final['roles'][0]['identity_state'] == 'current'
        correction = json.loads(final['history'][0]['snapshot_json'])['corrected_roles'][0]
        assert correction['before']['role'] == 'subject' and correction['after']['role'] == 'participant'


def test_changed_support_is_historical_and_new_stale_or_foreign_writes_fail(tmp_path, monkeypatch):
    monkeypatch.setenv('CASE_INTELLIGENCE_STORAGE_RESERVE_GIB', '0')
    with TestClient(_app(tmp_path / 'runtime')) as client:
        slug = _create_matter(client)
        _upload(client, slug)
        token, _, _ = _passage(client, slug, 'Morgan')
        entity_id = _entity(client, slug, 'Alex Example', support=token)
        assertion_id = _create_event(client, slug, entity_id, token)
        original = _detail(client, slug, assertion_id)
        other_slug = _create_matter(client, 'Other synthetic scope')
        other_id = _entity(client, other_slug, 'Other synthetic identity')
        foreign = client.post(f'/matters/{other_slug}/assertions/actions', data=dict(action='create',
            entity_id=other_id, entity_revision=1, role='subject', support=token,
            attributed_to='Unsaved foreign account', **EVENT_FIELDS))
        assert foreign.status_code == 400 and 'Unsaved foreign account' in foreign.text
        bench = client.app.state.workbench
        other_matter = bench.matter(other_slug, WEB_ACTOR)
        assert bench.assertion_service(other_matter).list(other_matter.matter_id, WEB_ACTOR)[1] == 0
        assert client.get(f'/matters/{other_slug}/assertions/{assertion_id}').status_code == 404
        bench = client.app.state.workbench
        matter = bench.matter(slug, WEB_ACTOR)
        store = bench.source_store(matter)
        with store.mutation_guard():
            document = store.get(original['accounts'][0]['document_id'])
            document.version_id = 'f' * 32
            store._save()
        historical = _detail(client, slug, assertion_id)
        assert not historical['accounts'][0]['available']
        assert historical['accounts'][0]['excerpt'] == original['accounts'][0]['excerpt']
        page = client.get(f'/matters/{slug}/assertions/{assertion_id}')
        assert page.status_code == 200 and historical['accounts'][0]['excerpt'] in page.text
        assert f'href="/matters/{slug}?support={token}' not in page.text
        rejected = _action(client, slug, assertion_id, 'attach', support=token,
            stance='competing', attributed_to='Unsaved stale account')
        assert rejected.status_code == 400 and 'Unsaved stale account' in rejected.text
        assert _detail(client, slug, assertion_id) == historical


def test_csrf_revoked_member_and_inaccessible_exports_fail_closed(tmp_path, monkeypatch):
    from tests.test_matter_management import OWNER, OTHER, _app as protected_app, _csrf, _headers, _principal_id
    from tests.test_matter_management import _create_matter as create_protected_matter
    monkeypatch.setenv('CASE_INTELLIGENCE_STORAGE_RESERVE_GIB', '0')
    with TestClient(protected_app(tmp_path), base_url=f"https://{LEGACY_STEM}.example.test") as client:
        csrf = _csrf(client.get('/matters/new', headers=_headers(OWNER)).text)
        slug = create_protected_matter(client, principal=OWNER, csrf_token=csrf, name='Synthetic protected assertions')
        _upload(client, slug, headers=_headers(OWNER), csrf=csrf)
        bench = client.app.state.workbench
        owner_id = _principal_id(client, OWNER)
        matter = bench.matter(slug, owner_id)
        document = next(iter(bench.source_store(matter).documents.values()))
        token = bench._support_token(bench._candidate(matter, document, document.parsed_units()[0], 1))
        entity = bench.entity_service(matter).create(matter.matter_id, owner_id, display_name='Alex Example', support=token)
        fields = dict(action='create', entity_id=entity['entity_id'], entity_revision=1, role='subject',
            support=token, attributed_to='Morgan Sample', **EVENT_FIELDS)
        path = f'/matters/{slug}/assertions'
        assert client.post(path + '/actions', headers=_headers(OWNER), data=fields).status_code == 403
        created = client.post(path + '/actions', headers=_headers(OWNER), data=dict(csrf_token=csrf, **fields), follow_redirects=False)
        assert created.status_code == 303
        assertion_id = urlparse(created.headers['location']).path.split('/')[-1]
        client.cookies.clear()
        client.get('/matters/new', headers=_headers(OTHER))
        member_id = _principal_id(client, OTHER)
        bench.workspace.add_member(matter.matter_id, member_id, owner_id)
        opened = client.get(path + '/' + assertion_id, headers=_headers(OTHER))
        assert opened.status_code == 200
        member_csrf = _csrf(opened.text)
        bench.workspace.revoke_member(matter.matter_id, member_id, owner_id)
        for url in (path + '/' + assertion_id, path + '/' + assertion_id + '/export',
                    f'/matters/{slug}/chronology', f'/matters/{slug}/chronology/export?format=json',
                    f'/matters/{slug}?support={token}', f'/matters/{slug}/export'):
            assert client.get(url, headers=_headers(OTHER)).status_code == 404
        denied = client.post(path + '/actions', headers=_headers(OTHER), data=dict(action='update',
            csrf_token=member_csrf, assertion_id=assertion_id, expected_revision=1, **EVENT_FIELDS))
        assert denied.status_code == 404
        assert bench.assertion_service(matter).detail(matter.matter_id, owner_id, assertion_id)['record']['revision'] == 1


def test_interrupted_purge_keeps_assertions_for_export_then_retries_cleanup(tmp_path, monkeypatch):
    monkeypatch.setenv('CASE_INTELLIGENCE_STORAGE_RESERVE_GIB', '0')
    with TestClient(_app(tmp_path / 'runtime')) as client:
        slug = _create_matter(client)
        _upload(client, slug)
        token, _, _ = _passage(client, slug, 'Morgan')
        entity_id = _entity(client, slug, 'Alex Example', support=token)
        assertion_id = _create_event(client, slug, entity_id, token)
        bench = client.app.state.workbench
        matter = bench.matter(slug, WEB_ACTOR)
        cleanup = bench._cleanup_matter_clip_exports
        def interrupted(_matter_id):
            raise RuntimeError('Synthetic interruption before source removal')
        monkeypatch.setattr(bench, '_cleanup_matter_clip_exports', interrupted)
        prepared, lifecycle = claim_bench_exit(bench, matter)
        with pytest.raises(WorkspaceProblem):
            bench.execute_matter_purge(prepared, lifecycle)
        assert bench.workspace.matter_lifecycle(matter.matter_id).state == 'purge_failed'
        exported = client.get(f'/matters/{slug}/export')
        assert exported.status_code == 200
        with zipfile.ZipFile(io.BytesIO(exported.content)) as archive:
            contents = '\n'.join(archive.read(name).decode() for name in archive.namelist()
                if name.startswith('assertions/') and name.endswith('.json'))
            assert assertion_id in contents and 'Morgan Sample' in contents
        monkeypatch.setattr(bench, '_cleanup_matter_clip_exports', cleanup)
        prepared, lifecycle = claim_bench_exit(bench, matter)
        bench.execute_matter_purge(prepared, lifecycle)
        assert bench.workspace.matter_lifecycle(matter.matter_id).state == 'deleted'
        assert not (bench.storage.matters / matter.matter_id).exists()


@pytest.fixture
def protected_assertion(tmp_path, monkeypatch):
    from tests.test_matter_management import OWNER, OTHER, _app as protected_app, _csrf, _headers, _principal_id
    from tests.test_matter_management import _create_matter as create_protected_matter
    monkeypatch.setenv('CASE_INTELLIGENCE_STORAGE_RESERVE_GIB', '0')
    with TestClient(protected_app(tmp_path), base_url=f"https://{LEGACY_STEM}.example.test") as client:
        csrf = _csrf(client.get('/matters/new', headers=_headers(OWNER)).text)
        slug = create_protected_matter(client, principal=OWNER, csrf_token=csrf, name='Synthetic in-flight revocation')
        _upload(client, slug, headers=_headers(OWNER), csrf=csrf)
        bench = client.app.state.workbench
        owner_id = _principal_id(client, OWNER)
        matter = bench.matter(slug, owner_id)
        document = next(iter(bench.source_store(matter).documents.values()))
        token = bench._support_token(bench._candidate(matter, document, document.parsed_units()[0], 1))
        entity = bench.entity_service(matter).create(matter.matter_id, owner_id, display_name='Alex Example', support=token)
        record = bench.assertion_service(matter).create(matter.matter_id, owner_id, support=token,
            attributed_to='Morgan Sample', roles=[dict(entity_id=entity['entity_id'], expected_revision=1, role='subject')],
            **EVENT_FIELDS)
        client.cookies.clear()
        client.get('/matters/new', headers=_headers(OTHER))
        member_id = _principal_id(client, OTHER)
        bench.workspace.add_member(matter.matter_id, member_id, owner_id)
        page = client.get(f'/matters/{slug}/assertions/{record["assertion_id"]}', headers=_headers(OTHER))
        assert page.status_code == 200
        yield dict(client=client, bench=bench, matter=matter, owner_id=owner_id, member_id=member_id,
            headers=_headers(OTHER), csrf=_csrf(page.text), entity=entity, record=record)


@pytest.mark.parametrize('format_name', ['json', 'markdown'])
def test_export_audit_distinguishes_each_assertion_and_matter_chronology(protected_assertion, format_name):
    context = protected_assertion
    client, bench, matter = (context[key] for key in ('client', 'bench', 'matter'))
    service = bench.assertion_service(matter)
    first_id = context['record']['assertion_id']
    detail = service.detail(matter.matter_id, context['owner_id'], first_id)
    second = service.create(matter.matter_id, context['owner_id'],
        support=detail['accounts'][0]['support_token'], attributed_to='Morgan Sample',
        roles=[dict(entity_id=context['entity']['entity_id'], expected_revision=1, role='subject')],
        **(EVENT_FIELDS | dict(title='Separate synthetic assertion')))
    expected = [('assertion', first_id), ('assertion', second['assertion_id']), ('matter', matter.matter_id)]
    for object_type, object_id in expected:
        path = (f'/matters/{matter.slug}/assertions/{object_id}/export'
                if object_type == 'assertion' else f'/matters/{matter.slug}/chronology/export')
        response = client.get(path, params=dict(format=format_name), headers=context['headers'])
        assert response.status_code == 200
    events = [event for event in bench.workspace.audit_events(matter.matter_id)
              if event.action == 'assertion.export']
    assert [(event.object_type, event.object_id) for event in events] == expected
    assert all(event.outcome == 'success' and event.actor_principal_id == context['member_id'] for event in events)
    assert all(not event.details for event in events)


@pytest.mark.parametrize('view', ['assertion', 'chronology', 'create', 'entity', 'stale_update'])
def test_midflight_html_membership_revocation_discards_saved_content_and_draft(protected_assertion, monkeypatch, view):
    from case_intelligence.entity_service import EntityService
    context = protected_assertion
    client, bench, matter = (context[key] for key in ('client', 'bench', 'matter'))
    entity_id, assertion_id = context['entity']['entity_id'], context['record']['assertion_id']
    original_list = EntityService.list
    revoked = []
    def list_then_revoke(service, *args, **kwargs):
        result = original_list(service, *args, **kwargs)
        if not revoked:
            bench.workspace.revoke_member(matter.matter_id, context['member_id'], context['owner_id'])
            revoked.append(True)
        return result
    monkeypatch.setattr(EntityService, 'list', list_then_revoke)
    prefix = f'/matters/{matter.slug}'
    paths = dict(assertion=prefix + '/assertions/' + assertion_id, chronology=prefix + '/chronology',
        create=prefix + '/assertions/new?entity_id=' + entity_id, entity=prefix + '/entities/' + entity_id)
    if view == 'stale_update':
        response = client.post(prefix + '/assertions/actions', headers=context['headers'], data=dict(
            action='update', csrf_token=context['csrf'], assertion_id=assertion_id, expected_revision=0,
            **(EVENT_FIELDS | dict(title='Unsaved revocation canary', statement='Discard this unsaved interpretation.'))))
    else:
        response = client.get(paths[view], headers=context['headers'])
    assert revoked and response.status_code == 404
    for text in ('Disputed parcel delivery', 'Unsaved revocation canary', 'Discard this unsaved interpretation.',
                 'Alex Example', 'Morgan Sample', ORIGINALS['Generated supporting account.txt']):
        assert text not in response.text


@pytest.mark.parametrize('view', ['assertion', 'chronology'])
def test_midflight_export_membership_revocation_discards_serialized_content(protected_assertion, monkeypatch, view):
    from case_intelligence import assertion_exports
    context = protected_assertion
    client, bench, matter = (context[key] for key in ('client', 'bench', 'matter'))
    original_export = assertion_exports.markdown_export
    revoked = []
    def export_then_revoke(payload):
        rendered = original_export(payload)
        bench.workspace.revoke_member(matter.matter_id, context['member_id'], context['owner_id'])
        revoked.append(True)
        return rendered
    monkeypatch.setattr(assertion_exports, 'markdown_export', export_then_revoke)
    path = (f'/matters/{matter.slug}/assertions/{context["record"]["assertion_id"]}/export'
            if view == 'assertion' else f'/matters/{matter.slug}/chronology/export')
    response = client.get(path, params=dict(format='markdown'), headers=context['headers'])
    assert revoked and response.status_code == 404
    assert 'Disputed parcel delivery' not in response.text and 'Morgan Sample' not in response.text
    assert 'Content-Disposition' not in response.headers
    assert bench.matter_active_work_counts(matter.matter_id)['exports'] == 0


def test_selected_competing_passage_survives_entity_role_search_add_and_correction(tmp_path, monkeypatch):
    monkeypatch.setenv('CASE_INTELLIGENCE_STORAGE_RESERVE_GIB', '0')
    with TestClient(_app(tmp_path / 'runtime')) as client:
        slug = _create_matter(client)
        prefix = f'/matters/{slug}'
        _upload(client, slug)
        supporting, _, _ = _passage(client, slug, 'Morgan')
        entity_id = _entity(client, slug, 'Alex Example', support=supporting)
        assertion_id = _create_event(client, slug, entity_id, supporting)
        depot_id = _entity(client, slug, 'Cedar Depot', entity_type='place', support=supporting)
        competing, _, source_page = _passage(client, slug, 'Riley')
        link = re.search(r'href="([^"]+/chronology\?support=[^"]+)"', source_page.text)
        assert link
        chronology_url = html.unescape(link.group(1))
        return_to = parse_qs(urlparse(chronology_url).query)['return_to'][0]
        chronology = client.get(chronology_url)
        choice = re.search(r'href="([^"]+/assertions/' + re.escape(assertion_id) + r'\?[^"]+)"', chronology.text)
        assert choice
        record_url = html.unescape(choice.group(1))
        assert parse_qs(urlparse(record_url).query)['support'] == [competing]
        opened = client.get(record_url)
        find_form = re.search(r'<form method="get">(.*?)</form>', opened.text, re.S)
        assert find_form and f'name="support" value="{competing}"' in find_form.group(1)
        searched = client.get(prefix + '/assertions/' + assertion_id,
            params=dict(q='Cedar', support=competing, return_to=return_to))
        assert searched.status_code == 200 and 'Selected original passage' in searched.text
        assert ORIGINALS['Generated competing account.txt'] in searched.text
        assert f'value="{depot_id}:1"' in searched.text
        detail = _detail(client, slug, assertion_id)
        initial_role = detail['roles'][0]
        for extra in (dict(entity_selection=f'{depot_id}:1', role='location'),
                      dict(entity_selection=f'{entity_id}:1', role='participant', role_id=initial_role['role_id'])):
            response = client.post(prefix + '/assertions/actions', data=dict(action='add_role',
                assertion_id=assertion_id, expected_revision=_detail(client, slug, assertion_id)['record']['revision'],
                support=competing, return_to=return_to, **extra), follow_redirects=False)
            assert response.status_code == 303
            returned = parse_qs(urlparse(response.headers['location']).query)
            assert returned['support'] == [competing] and returned['return_to'] == [return_to]
            page = client.get(response.headers['location'])
            assert ORIGINALS['Generated competing account.txt'] in page.text and 'Attach selected original' in page.text
        attached = client.post(prefix + '/assertions/actions', data=dict(action='attach',
            assertion_id=assertion_id, expected_revision=_detail(client, slug, assertion_id)['record']['revision'],
            support=competing, stance='competing', attributed_to='Riley Demo', return_to=return_to), follow_redirects=False)
        assert attached.status_code == 303
        final_query = parse_qs(urlparse(attached.headers['location']).query)
        assert not final_query.get('support') and final_query['return_to'] == [return_to]
        final = _detail(client, slug, assertion_id)
        assert {(role['entity_id'], role['role']) for role in final['roles']} == {
            (entity_id, 'participant'), (depot_id, 'location')}
        assert len(final['accounts']) == 2
        assert next(account for account in final['accounts'] if account['stance'] == 'competing')['support_token'] == competing


@pytest.mark.parametrize('view', ['assertion', 'chronology'])
@pytest.mark.parametrize('format_name', ['json', 'markdown'])
def test_serialized_export_limit_returns_no_partial_document(protected_assertion, monkeypatch, view, format_name):
    from case_intelligence import assertion_repository
    context = protected_assertion
    client, bench, matter = (context[key] for key in ('client', 'bench', 'matter'))
    # Apply a deliberately tiny outgoing-body budget after creating a normal
    # record. This exercises the serialized response guard, not admission limits.
    monkeypatch.setattr(assertion_repository, 'MAX_STORAGE_BYTES', 32)
    path = (f'/matters/{matter.slug}/assertions/{context["record"]["assertion_id"]}/export'
            if view == 'assertion' else f'/matters/{matter.slug}/chronology/export')
    response = client.get(path, params=dict(format=format_name), headers=context['headers'])
    assert response.status_code == 400 and 'No partial export' in response.text
    assert 'Disputed parcel delivery' not in response.text and 'Morgan Sample' not in response.text
    assert 'Content-Disposition' not in response.headers
    assert bench.matter_active_work_counts(matter.matter_id)['exports'] == 0


def test_removed_role_correction_form_does_not_turn_retry_into_role_creation(tmp_path, monkeypatch):
    monkeypatch.setenv('CASE_INTELLIGENCE_STORAGE_RESERVE_GIB', '0')
    with TestClient(_app(tmp_path / 'runtime')) as client:
        slug = _create_matter(client)
        _upload(client, slug)
        token, _, _ = _passage(client, slug, 'Morgan')
        entity_id = _entity(client, slug, 'Alex Example', support=token)
        depot_id = _entity(client, slug, 'Cedar Depot', entity_type='place', support=token)
        assertion_id = _create_event(client, slug, entity_id, token)
        assert _action(client, slug, assertion_id, 'add_role',
            entity_selection=f'{depot_id}:1', role='location').status_code == 200
        opened = _detail(client, slug, assertion_id)
        assert opened['record']['revision'] == 2
        removed_role = next(role for role in opened['roles'] if role['entity_id'] == entity_id)
        assert _action(client, slug, assertion_id, 'remove_role', role_id=removed_role['role_id']).status_code == 200
        stale = _action(client, slug, assertion_id, 'add_role', revision=2,
            role_id=removed_role['role_id'], entity_selection=f'{entity_id}:1', role='participant')
        assert stale.status_code == 409
        correction_form = next(form for form in re.findall(r'<form\b[^>]*>.*?</form>', stale.text, re.S)
            if 'name="action" value="add_role"' in form)
        role_select = re.search(r'<select name="role_id"[^>]*>(.*?)</select>', correction_form, re.S)
        assert role_select
        selected_role = re.search(r'<option value="([^"]*)"[^>]*\bselected\b[^>]*>', role_select.group(1))
        assert selected_role and html.unescape(selected_role.group(1)) == removed_role['role_id']
        revision = int(re.search(r'name="expected_revision" value="(\d+)"', correction_form).group(1))
        assert revision == 3
        # Re-submit the returned form's actual selected role and current record
        # revision. An unavailable target remains a correction, never an add.
        retry = _action(client, slug, assertion_id, 'add_role', revision=revision,
            role_id=html.unescape(selected_role.group(1)), entity_selection=f'{entity_id}:1', role='participant')
        assert retry.status_code == 409
        retained = _detail(client, slug, assertion_id)
        assert retained['record']['revision'] == 3
        assert [(role['entity_id'], role['role']) for role in retained['roles']] == [(depot_id, 'location')]
        # Choosing the separate add option is an explicit new user decision.
        added = _action(client, slug, assertion_id, 'add_role', role_id='',
            entity_selection=f'{entity_id}:1', role='participant')
        assert added.status_code == 200
        current = _detail(client, slug, assertion_id)
        replacement = next(role for role in current['roles'] if role['entity_id'] == entity_id)
        assert len(current['roles']) == 2 and replacement['role_id'] != removed_role['role_id']


def test_original_inspection_return_stays_inside_current_matter(tmp_path, monkeypatch):
    monkeypatch.setenv('CASE_INTELLIGENCE_STORAGE_RESERVE_GIB', '0')
    with TestClient(_app(tmp_path / 'runtime')) as client:
        slug = _create_matter(client)
        _upload(client, slug)
        token, _, _ = _passage(client, slug, 'Morgan')
        entity_id = _entity(client, slug, 'Alex Example', support=token)
        assertion_id = _create_event(client, slug, entity_id, token)
        prefix = f'/matters/{slug}'
        internal = prefix + '/assertions/' + assertion_id + '?' + urlencode(dict(return_to=prefix + '?q=Morgan#search-results'))
        for target, permitted in ((internal, True), ('https://outside.example.test/review', False),
                                  ('//outside.example.test/review', False), ('/matters/other-synthetic-scope/chronology', False)):
            pane = client.get(prefix, params=dict(support=token, entity_return_to=target))
            assert pane.status_code == 200
            assert ('Return to review context' in pane.text) is permitted
            full_link = re.search(r'class="support-header-open" href="([^"]+)"', pane.text)
            assert full_link
            full = client.get(html.unescape(full_link.group(1)))
            assert full.status_code == 200
            assert ('Return to review context' in full.text) is permitted
            if permitted:
                for page in (pane, full):
                    link = re.search(r'href="([^"]+)"[^>]*>Return to review context</a>', page.text)
                    assert link and html.unescape(link.group(1)) == internal
                    returned = client.get(html.unescape(link.group(1)))
                    assert returned.status_code == 200 and 'Disputed parcel delivery' in returned.text
