"""HTTP state, authorization, export and stopped-runtime recovery regressions."""
import csv
import io
import json
import shutil
from unittest.mock import patch

from fastapi.testclient import TestClient
from case_intelligence.generation import UnavailableGenerator
from case_intelligence.workbench import create_workbench_app
from case_intelligence.review_candidates import FORMAT, NOTICE
from tests.test_matter_notebook import WEB_ACTOR, _create_matter

TEXT='The witness said no deal was offered during the interview.'


def app(root):
    return create_workbench_app(root,generator=UnavailableGenerator(),auth_mode='test')


def seed(client):
    slug=_create_matter(client,'Synthetic candidate review')
    assert client.post(f'/matters/{slug}/uploads',files=[('files',('Synthetic-review.txt',TEXT.encode(),'text/plain'))]).status_code==200
    payload=client.get(f'/matters/{slug}/review-candidates/export').json()
    assert payload['candidates'], payload
    assert payload['prior_statement_difference_enabled'] is False
    return slug,payload['candidates'][0]


def test_review_persists_exports_and_clean_restore(tmp_path):
    runtime=tmp_path/'runtime'
    with TestClient(app(runtime)) as client:
        slug,candidate=seed(client); prefix=f'/matters/{slug}/review-candidates'
        response=client.get(prefix)
        assert response.status_code==200 and '<mark>deal</mark>' in response.text and NOTICE in response.text
        assert 'Candidates for review' in response.text
        url=prefix+'/'+candidate['signature']+'/review'
        assert client.post(url,data=dict(action='dismiss',revision=0),follow_redirects=False).status_code==303
        assert 'class="review-candidate-card"' not in client.get(prefix).text
        assert 'class="review-candidate-card"' in client.get(prefix+'?status=dismissed').text
        assert client.post(url,data=dict(action='reviewed',revision=0)).status_code==409
        assert client.post(url,data=dict(action='reviewed',revision=1)).status_code==200
        expected=client.get(prefix+'/export').json()
        assert expected['format']==FORMAT and expected['notice']==NOTICE
        assert expected['candidates'][0]['reviewed_by']==WEB_ACTOR and expected['candidates'][0]['reviewed_at']
        for kind in ('markdown','json','csv'):
            response=client.get(prefix+'/export?format='+kind)
            assert response.status_code==200 and 'no-store' in response.headers['cache-control']
            extension='md' if kind=='markdown' else kind
            assert response.headers['content-disposition']==f'attachment; filename="review-candidates.{extension}"'
            assert 'Synthetic-review.txt' in response.text.replace('\\','') and candidate['citation']['location'] in response.text
            if kind=='markdown': assert response.text.startswith(NOTICE)
            if kind=='json': assert next(iter(response.json()))=='notice'
            if kind=='csv':
                rows=list(csv.reader(io.StringIO(response.text)))
                assert rows[0]==[NOTICE] and rows[3][9]==TEXT
        assert not client.get(prefix+'/export?category=alternate_person').json()['candidates']
        assert client.get(prefix+'/export?category=witness_benefit').json()['candidates']
        assert not client.get(prefix+'/export?status=dismissed').json()['candidates']
        assert client.get(prefix+'/export?format=html').status_code==422
    backup=tmp_path/'backup'; restored=tmp_path/'restored'
    shutil.copytree(runtime,backup); shutil.copytree(backup,restored)
    runtime.rename(tmp_path/'offline-original'); backup.rename(tmp_path/'offline-backup')
    with TestClient(app(restored)) as client:
        assert client.get(prefix+'/export').json()==expected
        assert client.post(url,data=dict(action='reopen',revision=2)).status_code==200
        assert client.get(prefix+'/export').json()['candidates'][0]['status']=='open'
        workspace=client.app.state.workbench.workspace
        with workspace._lock:
            assert workspace.connection.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
            assert not workspace.connection.execute('PRAGMA foreign_key_check').fetchall()


def test_cross_matter_invalid_action_and_current_source_checks(tmp_path):
    with TestClient(app(tmp_path)) as client:
        slug,candidate=seed(client); other=_create_matter(client,'Other synthetic matter')
        assert client.post(f'/matters/{other}/review-candidates/{candidate["signature"]}/review',data=dict(action='reviewed',revision=0)).status_code==404
        url=f'/matters/{slug}/review-candidates/{candidate["signature"]}/review'
        assert client.post(url,data=dict(action='unsupported',revision=0)).status_code==400
        bench=client.app.state.workbench; matter=bench.matter(slug,WEB_ACTOR); store=bench.source_store(matter)
        with patch.object(store,'ready_documents',return_value=()):
            assert client.post(url,data=dict(action='reviewed',revision=0)).status_code==404
            assert not client.get(f'/matters/{slug}/review-candidates/export').json()['candidates']
        from case_intelligence.candidate_review import CandidateReviewRepository
        from case_intelligence.review_candidate_service import ReviewCandidateService
        service=ReviewCandidateService(bench,matter)
        with service.snapshot(WEB_ACTOR,writing=True) as snapshot:
            c=snapshot['candidates'][0]
            service.reviews.save(matter.matter_id,WEB_ACTOR,c,'dismiss',0,namespace='inconsistency')
        assert client.get(f'/matters/{slug}/review-candidates/export').json()['candidates'][0]['status']=='open'


def test_protected_csrf_and_revoked_member(tmp_path,monkeypatch):
    from tests.test_matter_management import OWNER,OTHER,_app as protected_app,_csrf,_headers,_principal_id
    from tests.test_matter_management import _create_matter as create_protected
    monkeypatch.setenv('CASE_INTELLIGENCE_STORAGE_RESERVE_GIB','0')
    with TestClient(protected_app(tmp_path),base_url='https://exculpata.example.test') as client:
        csrf=_csrf(client.get('/matters/new',headers=_headers(OWNER)).text)
        slug=create_protected(client,principal=OWNER,csrf_token=csrf,name='Synthetic protected candidates')
        assert client.post(f'/matters/{slug}/uploads',headers=_headers(OWNER),data=dict(csrf_token=csrf),files=[('files',('Synthetic-review.txt',TEXT.encode(),'text/plain'))]).status_code==200
        prefix=f'/matters/{slug}/review-candidates'
        candidate=client.get(prefix+'/export',headers=_headers(OWNER)).json()['candidates'][0]
        url=prefix+'/'+candidate['signature']+'/review'; fields=dict(action='reviewed',revision=0)
        assert client.post(url,headers=_headers(OWNER),data=fields).status_code==403
        assert client.post(url,headers=_headers(OWNER),data=dict(fields,csrf_token=csrf),follow_redirects=False).status_code==303
        bench=client.app.state.workbench; owner=_principal_id(client,OWNER); matter=bench.matter(slug,owner)
        client.cookies.clear(); client.get('/matters/new',headers=_headers(OTHER)); member=_principal_id(client,OTHER)
        bench.workspace.add_member(matter.matter_id,member,owner)
        page=client.get(prefix,headers=_headers(OTHER)); assert page.status_code==200
        member_csrf=_csrf(page.text); bench.workspace.revoke_member(matter.matter_id,member,owner)
        for path in (prefix,prefix+'/export',prefix+'/export?format=csv',prefix+'/export?format=markdown'):
            assert client.get(path,headers=_headers(OTHER)).status_code==404
        assert client.post(url,headers=_headers(OTHER),data=dict(fields,csrf_token=member_csrf,revision=1)).status_code==404


def test_review_state_full_archive_freeze_and_purge(tmp_path):
    import sqlite3
    import zipfile
    import pytest
    from tests.case_lifecycle_web_helpers import claim_bench_exit
    from case_intelligence.case_lifecycle_support import archive_path
    with TestClient(app(tmp_path)) as client:
        slug,candidate=seed(client); prefix=f'/matters/{slug}/review-candidates'
        assert client.post(prefix+'/'+candidate['signature']+'/review',data=dict(action='reviewed',revision=0)).status_code==200
        bench=client.app.state.workbench; matter=bench.matter(slug,WEB_ACTOR)
        prepared,lifecycle=claim_bench_exit(bench,matter)
        current=bench.workspace.case_lifecycle.current_exit(matter.matter_id)
        with zipfile.ZipFile(archive_path(bench,matter,current)) as archive:
            rows=archive.read('records/workbench_candidate_review.jsonl').decode()
            assert candidate['signature'] in rows and 'review_candidates' in rows
        with bench.workspace._lock,pytest.raises(sqlite3.IntegrityError):
            bench.workspace.connection.execute("UPDATE workbench_candidate_review SET status='dismissed' WHERE matter_id=?",(matter.matter_id,))
        bench.execute_matter_purge(prepared,lifecycle)
        with bench.workspace._lock:
            assert not bench.workspace.connection.execute('SELECT * FROM workbench_candidate_review WHERE matter_id=?',(matter.matter_id,)).fetchall()


def test_optional_model_rechecks_sources_without_holding_workspace_lock(tmp_path,monkeypatch):
    from case_intelligence.generation import GroundedGenerationService
    from case_intelligence.review_candidate_service import ReviewCandidateService
    from tests.test_review_candidates import SyntheticGenerator
    with TestClient(app(tmp_path)) as client:
        slug,_=seed(client); bench=client.app.state.workbench; matter=bench.matter(slug,WEB_ACTOR)
        service=ReviewCandidateService(bench,matter)
        class SourceChanges(SyntheticGenerator):
            def generate(self,**kwargs):
                assert not bench.workspace._lock._is_owned()
                monkeypatch.setattr(service.store,'ready_documents',lambda:())
                return super().generate(**kwargs)
        payload=service.payload(WEB_ACTOR,generation=GroundedGenerationService(SourceChanges(TEXT)))
        assert payload['candidates']==[]


def test_route_reports_cap_and_keeps_category_source_order(tmp_path,monkeypatch):
    import case_intelligence.review_candidate_service as module
    from dataclasses import replace
    from tests.test_review_candidates import detect,unit
    with TestClient(app(tmp_path)) as client:
        slug,_=seed(client)
        candidate=detect([unit(TEXT)])[0]
        rows=[replace(candidate,signature=str(i),source_order=i) for i in range(151)]
        monkeypatch.setattr(module,'validate',lambda candidates,resolve:(rows,0))
        payload=client.get(f'/matters/{slug}/review-candidates/export').json()
        assert payload['capped'] and len(payload['candidates'])==150
        assert [c['source_order'] for c in payload['candidates']]==list(range(150))


def test_streaming_scan_stops_before_next_unit_or_source(tmp_path,monkeypatch):
    from dataclasses import replace
    from case_intelligence.pilot_uploads import PilotUnit
    from case_intelligence.review_candidate_service import ReviewCandidateService, MAX_RECORD_CHARS
    from case_intelligence.review_candidates import MAX_UNITS
    from case_intelligence.unit_stream import iter_unit_records
    with TestClient(app(tmp_path)) as client:
        slug,_=seed(client); bench=client.app.state.workbench
        service=ReviewCandidateService(bench,bench.matter(slug,WEB_ACTOR))
        source=service.store.ready_documents()[0]
        stream=io.StringIO(json.dumps(dict(version=1,units=[dict(number=i,text='Synthetic neutral passage.') for i in range(1,MAX_UNITS+5001)])))
        decoded=[]
        def iterator(name,**kwargs):
            assert kwargs['max_record_chars']==MAX_RECORD_CHARS
            for record in iter_unit_records(stream,**kwargs):
                decoded.append(record['number'])
                yield PilotUnit(**record)
        def whole_file(name):
            raise AssertionError('whole-file loading is forbidden for candidate scans')
        source=replace(source,units=[],units_file='synthetic.json',_units_iterator=iterator,_units_loader=whole_file)
        later=replace(source,_units_iterator=lambda *args,**kwargs:whole_file('later source'))
        monkeypatch.setattr(service.store,'ready_documents',lambda:(source,later))
        payload=service.payload(WEB_ACTOR)
        assert payload['capped'] and payload['unit_count']==MAX_UNITS
        assert len(decoded)==MAX_UNITS and stream.tell()<len(stream.getvalue())


def test_streaming_record_and_serialized_limits_report_partial_scan(tmp_path,monkeypatch):
    from dataclasses import replace
    from case_intelligence.pilot_uploads import PilotUnit
    import case_intelligence.review_candidate_service as module
    from case_intelligence.unit_stream import iter_unit_records
    with TestClient(app(tmp_path)) as client:
        slug,_=seed(client); bench=client.app.state.workbench
        service=module.ReviewCandidateService(bench,bench.matter(slug,WEB_ACTOR))
        source=service.store.ready_documents()[0]
        for oversized in (True,False):
            text='x'*(module.MAX_RECORD_CHARS+1) if oversized else TEXT
            stream=io.StringIO(json.dumps(dict(version=1,units=[dict(number=1,text=text)])))
            decoded=[]
            def iterator(name,**kwargs):
                for record in iter_unit_records(stream,**kwargs):
                    decoded.append(record)
                    yield PilotUnit(**record)
            bounded=replace(source,units=[],units_file='synthetic.json',_units_iterator=iterator)
            monkeypatch.setattr(service.store,'ready_documents',lambda:(bounded,))
            if not oversized:
                monkeypatch.setattr(module,'MAX_SERIALIZED_CHARS',10)
            payload=service.payload(WEB_ACTOR)
            assert payload['capped'] and payload['unit_count']==0 and not decoded


def test_repeated_trigger_highlights_the_nonnegated_occurrence(tmp_path):
    text='The DNA source could not be conclusively excluded, but the fingerprint excluded the comparison source.'
    with TestClient(app(tmp_path)) as client:
        slug=_create_matter(client,'Synthetic comparison review')
        assert client.post(f'/matters/{slug}/uploads',files=[('files',('Synthetic-comparison.txt',text.encode(),'text/plain'))]).status_code==200
        response=client.get(f'/matters/{slug}/review-candidates')
        assert 'conclusively excluded, but the fingerprint <mark>excluded</mark>' in response.text
