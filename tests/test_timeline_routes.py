"""Live synthetic authorization, source changes and promotion contracts."""
import pytest
from fastapi.testclient import TestClient
from case_intelligence.workbench import create_workbench_app
from case_intelligence.generation import UnavailableGenerator
from tests.test_matter_notebook import _create_matter, WEB_ACTOR

@pytest.fixture
def timeline_client(tmp_path,monkeypatch):
    monkeypatch.setenv('CASE_INTELLIGENCE_STORAGE_RESERVE_GIB','0')
    with TestClient(create_workbench_app(tmp_path,auth_mode='test',generator=UnavailableGenerator())) as client:
        slug=_create_matter(client,'Synthetic timeline review')
        data={'alpha.txt':'Alex Example delivered the parcel at Cedar Depot on April 18, 2034.',
              'beta.txt':'Alex Example delivered the parcel at Cedar Depot on April 19, 2034.',
              'relative.txt':'Record date: 2034-04-18.\n\nThe next morning Alex Example inspected the parcel.'}
        result=client.post(f'/matters/{slug}/uploads',files=[('files',(name,text.encode(),'text/plain')) for name,text in data.items()])
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
    assert client.post(prefix+'/refresh').status_code==200
    assert 'Refresh completed' in client.get(prefix).text

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
