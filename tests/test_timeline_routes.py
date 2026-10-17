"""Live synthetic authorization, source changes and promotion contracts."""
import pytest
from fastapi.testclient import TestClient
from case_intelligence.workbench import create_workbench_app
from case_intelligence.generation import UnavailableGenerator
from tests.test_matter_notebook import _create_matter, WEB_ACTOR
from tests.test_workflow_quality_handoff import pdf_bytes

@pytest.fixture
def timeline_client(tmp_path,monkeypatch):
    monkeypatch.setenv('CASE_INTELLIGENCE_STORAGE_RESERVE_GIB','0')
    with TestClient(create_workbench_app(tmp_path,auth_mode='test',generator=UnavailableGenerator())) as client:
        slug=_create_matter(client,'Synthetic timeline review')
        data={'alpha.txt':'Alex Example delivered the parcel at Cedar Depot on April 18, 2034.',
              'beta.txt':'Alex Example delivered the parcel at Cedar Depot on April 19, 2034.'}
        files=[('files',(name,text.encode(),'text/plain')) for name,text in data.items()]
        files.append(('files', ('relative.pdf', pdf_bytes(['Record date: 2034-04-18.',
            'The next morning Alex Example inspected the parcel.']), 'application/pdf')))
        result=client.post(f'/matters/{slug}/uploads',files=files)
        assert result.status_code==200
        yield client,slug

def test_export_banner_promote_refresh(timeline_client):
    client,slug=timeline_client
    prefix=f'/matters/{slug}/timeline/draft'
    response=client.get(prefix)
    assert response.status_code==200 and 'for attorney review' in response.text
    for format in ['json','csv','markdown']:
        response=client.get(prefix+'/export',params={'format':format})
        assert response.status_code==200 and 'alpha.txt' in response.text
        assert 'for attorney review' in response.headers['x-review-notice']
    payload=client.get(prefix+'/export').json()
    assert len(payload['conflicts'])==1
    entry=next(e for e in payload['entries'] if e['date_label']=='The next morning')
    entity=client.post(f'/matters/{slug}/entities/actions',data={'action':'create','display_name':'Alex Example','entity_type':'person'},follow_redirects=False)
    assert entity.status_code==303
    entity_id=entity.headers['location'].split('?')[0].split('/')[-1]
    promoted=client.post(prefix+'/'+entry['entry_id']+'/promote',data={'entity_selection':entity_id+':1'},follow_redirects=False)
    assert promoted.status_code==303,promoted.text
    bench=client.app.state.workbench
    matter=bench.matter(slug,WEB_ACTOR)
    detail=bench.assertion_service(matter).detail(matter.matter_id,WEB_ACTOR,promoted.headers['location'].split('/')[-1])
    assert detail['record']['sort_date']=='2034-04-19'
    assert detail['record']['raw_date']=='The next morning'
    assert detail['record']['status']=='needs_review'
    assert detail['accounts'] and all(a['available'] for a in detail['accounts'])
    assert len(detail['accounts']) == 2
    assert {a['support_token'] for a in detail['accounts']} == {c['support_token'] for c in entry['citations']}
    assert {a['unit_number'] for a in detail['accounts']} == {1, 2}
    assert client.post(prefix+'/refresh').status_code==200
    assert 'Refresh completed' in client.get(prefix).text

def test_anchor_attachment_failure_rolls_back_entire_promotion(timeline_client, monkeypatch):
    from case_intelligence.assertion_repository import AssertionRepository
    from case_intelligence.workspace_store import WorkspaceProblem
    client, slug = timeline_client
    prefix = f'/matters/{slug}/timeline/draft'
    entry = next(e for e in client.get(prefix+'/export').json()['entries'] if e['date_label']=='The next morning')
    entity = client.post(f'/matters/{slug}/entities/actions', data={
        'action':'create', 'display_name':'Alex Example', 'entity_type':'person'}, follow_redirects=False)
    entity_id = entity.headers['location'].split('?')[0].split('/')[-1]
    original = AssertionRepository.add_account
    def fail_anchor(repo, *args, **kwargs):
        result = original(repo, *args, **kwargs)
        if len(repo.accounts(args[0], args[2])) == 2:
            raise WorkspaceProblem('Synthetic anchor write failure')
        return result
    monkeypatch.setattr(AssertionRepository, 'add_account', fail_anchor)
    response = client.post(prefix+'/'+entry['entry_id']+'/promote',
        data={'entity_selection':entity_id+':1'}, follow_redirects=False)
    assert response.status_code == 409
    bench = client.app.state.workbench
    matter = bench.matter(slug, WEB_ACTOR)
    assert bench.assertion_service(matter).list(matter.matter_id, WEB_ACTOR)[1] == 0

def test_cross_matter_entry_and_entity_rejected(timeline_client):
    client,slug=timeline_client
    prefix=f'/matters/{slug}/timeline/draft'
    entry=client.get(prefix+'/export').json()['entries'][0]
    other=_create_matter(client,'Another synthetic matter')
    assert client.post(f'/matters/{other}/timeline/draft/'+entry['entry_id']+'/promote',data={'entity_selection':'not-present:1'}).status_code==404
    assert client.get('/matters/not-authorized/timeline/draft').status_code==404
    assert client.get('/matters/not-authorized/timeline/draft/export').status_code==404
    assert client.post(prefix+'/'+entry['entry_id']+'/promote',data={'entity_selection':'not-present:1'}).status_code==404

def test_export_lease_and_csrf_dependencies(timeline_client):
    client,slug=timeline_client
    routes={r.path:r for r in client.app.routes if hasattr(r,'dependant')}
    export=routes['/matters/{slug}/timeline/draft/export']
    assert any('lease' in d.call.__name__ for d in export.dependant.dependencies)
    for path in ['/matters/{slug}/timeline/draft/refresh','/matters/{slug}/timeline/draft/{entry_id}/promote']:
        assert any('csrf' in d.call.__name__ for d in routes[path].dependant.dependencies)

def test_actual_csrf_and_revoked_membership(tmp_path, monkeypatch):
    from tests.test_matter_management import OWNER, OTHER, _app, _csrf, _headers, _principal_id
    from tests.test_matter_management import _create_matter as create_protected
    monkeypatch.setenv('CASE_INTELLIGENCE_STORAGE_RESERVE_GIB','0')
    with TestClient(_app(tmp_path),base_url='https://exculpata.example.test') as client:
        csrf=_csrf(client.get('/matters/new',headers=_headers(OWNER)).text)
        slug=create_protected(client,principal=OWNER,csrf_token=csrf,name='Protected synthetic timeline')
        prefix=f'/matters/{slug}/timeline/draft'
        assert client.post(prefix+'/refresh',headers=_headers(OWNER)).status_code==403
        assert client.post(prefix+'/missing/promote',headers=_headers(OWNER),data={'entity_selection':'missing:1'}).status_code==403
        bench=client.app.state.workbench
        owner=_principal_id(client,OWNER)
        matter=bench.matter(slug,owner)
        client.cookies.clear()
        client.get('/matters/new',headers=_headers(OTHER))
        member=_principal_id(client,OTHER)
        bench.workspace.add_member(matter.matter_id,member,owner)
        opened=client.get(prefix,headers=_headers(OTHER))
        assert opened.status_code==200
        csrf=_csrf(opened.text)
        bench.workspace.revoke_member(matter.matter_id,member,owner)
        assert client.get(prefix,headers=_headers(OTHER)).status_code==404
        for format in ('json','csv','markdown'):
            assert client.get(prefix+'/export',params={'format':format},headers=_headers(OTHER)).status_code==404
        assert client.post(prefix+'/refresh',headers=_headers(OTHER),data={'csrf_token':csrf}).status_code==404
        assert client.post(prefix+'/missing/promote',headers=_headers(OTHER),data={'csrf_token':csrf,'entity_selection':'missing:1'}).status_code==404

def test_lease_denial_prevents_export(timeline_client,monkeypatch):
    from case_intelligence.workspace_store import WorkspaceProblem
    client,slug=timeline_client
    def denied(*args,**kwargs):
        raise WorkspaceProblem('Synthetic lease denial')
    monkeypatch.setattr(client.app.state.workbench,'begin_matter_response',denied)
    for format in ('csv','markdown','json'):
        response=client.get(f'/matters/{slug}/timeline/draft/export',params={'format':format})
        assert response.status_code==409 and 'alpha.txt' not in response.text

def test_source_version_change_rejects_stale_promotion(timeline_client):
    client,slug=timeline_client
    prefix=f'/matters/{slug}/timeline/draft'
    entry=client.get(prefix+'/export').json()['entries'][0]
    bench=client.app.state.workbench
    matter=bench.matter(slug,WEB_ACTOR)
    store=bench.source_store(matter)
    with store.mutation_guard():
        store.get(entry['citations'][0]['source_id']).version_id='f'*32
        store._save()
    assert client.post(prefix+'/'+entry['entry_id']+'/promote',data={'entity_selection':'missing:1'}).status_code==404
    assert bench.assertion_service(matter).list(matter.matter_id,WEB_ACTOR)[1]==0

@pytest.mark.parametrize('total', [2500, 2501, 2700])
def test_live_cutoff_counts_all_ready_passages_and_exports(timeline_client, monkeypatch, total):
    from dataclasses import replace
    client, slug = timeline_client
    bench = client.app.state.workbench
    store = bench.source_store(bench.matter(slug, WEB_ACTOR))
    originals = list(store.ready_documents())
    # Split across documents: the population count must continue after the
    # collection ceiling, without constructing more than 2,501 candidates.
    documents = []
    for original, count in zip(originals, (2501 if total > 2500 else 2500, max(0, total-2501))):
        document = replace(original, units=[{'number': i+1, 'text': 'No dated event here.'} for i in range(count)], units_file='')
        documents.append(document)
    monkeypatch.setattr(store, 'ready_documents', lambda: tuple(documents))
    original_candidate = bench._candidate
    calls = []
    def candidate(*args):
        calls.append(1)
        return original_candidate(*args)
    monkeypatch.setattr(bench, '_candidate', candidate)
    prefix = f'/matters/{slug}/timeline/draft'
    notice = f'Included 2,500 of {total:,} passages from ready sources in the date scan.'
    for endpoint in ('', '/export?format=csv', '/export?format=markdown', '/export?format=json'):
        calls.clear()
        response = client.get(prefix + endpoint)
        assert response.status_code == 200
        assert notice in response.text
        assert ('not a complete chronology' in response.text) is (total > 2500)
        assert len(calls) == min(total, 2501)
        if endpoint.endswith('json'):
            assert response.json()['total_unit_count'] == total
            assert response.json()['unit_count'] == 2500
            assert response.json()['capped'] is (total > 2500)

def test_coverage_population_scan_is_bounded(timeline_client, monkeypatch):
    from dataclasses import replace
    client, slug = timeline_client
    bench = client.app.state.workbench
    store = bench.source_store(bench.matter(slug, WEB_ACTOR))
    original = next(iter(store.ready_documents()))
    document = replace(original, units=[{'number': i+1, 'text': 'No dated event.'} for i in range(10001)], units_file='')
    monkeypatch.setattr(store, 'ready_documents', lambda: (document,))
    response = client.get(f'/matters/{slug}/timeline/draft/export')
    assert response.status_code == 200
    payload = response.json()
    assert payload['total_unit_count'] is None
    assert payload['capped']
    assert 'Total passage count is unavailable' in payload['coverage_notice']

@pytest.mark.parametrize('failure', ['missing', 'corrupt', 'oversized'])
def test_unreadable_tail_preserves_draft_with_unknown_total(timeline_client, monkeypatch, failure):
    from dataclasses import replace
    client, slug = timeline_client
    bench = client.app.state.workbench
    store = bench.source_store(bench.matter(slug, WEB_ACTOR))
    first, tail, *_ = store.ready_documents()
    first = replace(first, units=[{'number': i+1, 'text': 'No dated event.'} for i in range(2501)], units_file='')
    name = 'e' * 32 + '.json'
    tail = replace(tail, units=[], units_file=name, _units_iterator=store._iter_units)
    path = store.derived / name
    if failure == 'missing':
        path.unlink(missing_ok=True)
    else:
        path.write_text('{broken' if failure == 'corrupt' else
                        '{"version":1,"units":[{"number":1,"text":"' + 'x' * 70000 + '"}]}')
    monkeypatch.setattr(store, 'ready_documents', lambda: (first, tail))
    prefix = f'/matters/{slug}/timeline/draft'
    for endpoint in ('', '/export?format=csv', '/export?format=markdown', '/export?format=json'):
        response = client.get(prefix + endpoint)
        assert response.status_code == 200
        assert 'Total passage count is unavailable' in response.text
        assert 'Included 2,500 passages' in response.text
        if endpoint.endswith('json'):
            assert response.json()['total_unit_count'] is None
            assert response.json()['capped']
