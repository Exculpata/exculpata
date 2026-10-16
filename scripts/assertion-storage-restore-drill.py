#!/usr/bin/env python3
"""Synthetic complete-runtime slice-18 upgrade, clean restore and old-reader rollback.

No installed node, model request, external service, or private material is used.
The stopped fixture runtime contains its control store, session state, managed
originals and source registries; every file is hashed before the clean copy.
"""
import argparse
from datetime import datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parents[1]
ACTOR = 'development-taylor-morgan'
BASELINE = 'e9ab89c792b562c2abf27715d15b5f70d3216900'

BASELINE_SCRIPT = r'''
import json,sys
from pathlib import Path
from fastapi.testclient import TestClient
from case_intelligence.workbench import create_workbench_app
actor='development-taylor-morgan'
runtime, receipt, phase = Path(sys.argv[1]),Path(sys.argv[2]),sys.argv[3]
with TestClient(create_workbench_app(runtime,auth_mode='test')) as client:
    if phase=='seed':
        response=client.post('/matters',data={'name':'Synthetic assertion recovery','descriptor':'Generated evidence only'},follow_redirects=False)
        assert response.status_code==303
        slug=response.headers['location'].split('/')[2]
        for name,body in [('Synthetic-account-A.txt',b'Alex Example reported Blair Sample delivered the synthetic parcel around the first Friday in May.'),('Synthetic-account-B.txt',b'Casey Specimen reported Blair Sample never delivered the synthetic parcel.')]:
            assert client.post('/matters/'+slug+'/uploads',files=[('files',(name,body,'text/plain'))]).status_code==200
        bench=client.app.state.workbench
        matter=bench.matter(slug,actor)
        sources=bench.source_store(matter)
        tokens=[bench._support_token(bench._candidate(matter,document,document.parsed_units()[0],1)) for document in sorted(sources.documents.values(),key=lambda row:row.display_name)]
        entity=bench.entity_service(matter).create(matter.matter_id,actor,display_name='Alex Example',aliases='A. Example',support=tokens[0])
        note,_=bench.workspace.create_notebook_item(matter.matter_id,actor,item_type='issue',status='needs_review',title='Synthetic recovery note',body='Original independent commentary')
        data=dict(slug=slug,matter_id=matter.matter_id,entity=bench.entity_service(matter).export(matter.matter_id,actor,entity['entity_id']),tokens=tokens,note_id=note.item_id)
        receipt.write_text(json.dumps(data))
    else:
        data=json.loads(receipt.read_text())
        bench=client.app.state.workbench
        matter=bench.matter(data['slug'],actor)
        assert bench.entity_service(matter).export(matter.matter_id,actor,data['entity']['entity']['entity_id'])==data['entity']
        assert bench.workspace.notebook_item(matter.matter_id,actor,data['note_id']).body=='Original independent commentary'
        assert not bench.workspace.connection.execute("SELECT 1 FROM sqlite_master WHERE name='workbench_assertion'").fetchone()
        for token in data['tokens']:
            assert client.get('/matters/'+data['slug']+'?support='+token).status_code==200
        sources=bench.source_store(matter)
        for document in sources.documents.values():
            response=client.get('/matters/'+data['slug']+'/sources/'+sources.action_token(document)+'/content')
            assert response.status_code==200 and b'synthetic parcel' in response.content
    assert bench.workspace.connection.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
    assert not bench.workspace.connection.execute('PRAGMA foreign_key_check').fetchall()
'''


def manifest(directory):
    result = {}
    for path in sorted(directory.rglob('*')):
        if path.is_symlink():
            raise RuntimeError('Synthetic backup unexpectedly contains a symlink')
        if path.is_file():
            result[str(path.relative_to(directory))] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def stopped_copy(source, destination):
    expected = manifest(source)
    if destination.exists():
        raise RuntimeError('Clean restore target must be absent')
    shutil.copytree(source, destination)
    assert manifest(destination) == expected
    return expected


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--baseline-ref', default=BASELINE)
    args = parser.parse_args()
    baseline = subprocess.check_output(['git', 'rev-parse', args.baseline_ref + '^{commit}'], cwd=ROOT, text=True).strip()
    with tempfile.TemporaryDirectory(prefix='recordbench-assertion-recovery-') as temporary:
        root = Path(temporary).resolve()
        archive_path = root / 'baseline.tar'
        with archive_path.open('wb') as output:
            subprocess.run(['git', 'archive', baseline, 'src'], cwd=ROOT, stdout=output, check=True)
        with tarfile.open(archive_path) as archive:
            archive.extractall(root / 'baseline', filter='data')
        # Bounded synthetic fixtures use no protected production disk reserve.
        env = {**os.environ, 'PYTHONPATH': str(root / 'baseline/src'), 'CASE_INTELLIGENCE_STORAGE_RESERVE_GIB': '0'}
        runtime, seed_receipt = root / 'working-runtime', root / 'synthetic-seed.json'
        subprocess.run([sys.executable, '-c', BASELINE_SCRIPT, str(runtime), str(seed_receipt), 'seed'], env=env, check=True)
        seed = json.loads(seed_receipt.read_text())
        preupgrade = root / 'preupgrade-backup'
        preupgrade_manifest = stopped_copy(runtime, preupgrade)
        sys.path.insert(0, str(ROOT / 'src'))
        os.environ['CASE_INTELLIGENCE_STORAGE_RESERVE_GIB'] = '0'
        os.environ['CASE_INTELLIGENCE_TEST_MODE'] = '1'
        from fastapi.testclient import TestClient
        from case_intelligence.workbench import create_workbench_app
        from case_intelligence.workspace_store import WorkspaceProblem
        from case_intelligence.assertion_repository import TABLES
        with TestClient(create_workbench_app(runtime, auth_mode='test')) as client:
            bench = client.app.state.workbench
            matter = bench.matter(seed['slug'], ACTOR)
            entity = seed['entity']['entity']
            assert bench.entity_service(matter).export(matter.matter_id, ACTOR, entity['entity_id']) == seed['entity']
            service = bench.assertion_service(matter)
            record = service.create(matter.matter_id, ACTOR, title='Synthetic conflicting delivery accounts',
                record_type='event', statement='Account A reports a delivery that account B denies.',
                raw_date='around the first Friday in May', date_uncertainty='Year and exact day are not stated.',
                support=seed['tokens'][0], attributed_to='Alex Example in account A',
                roles=[dict(entity_id=entity['entity_id'], expected_revision=entity['revision'], role='speaker')])
            record = service.attach(matter.matter_id, ACTOR, record['assertion_id'], expected_revision=1,
                support=seed['tokens'][1], stance='competing', attributed_to='Casey Specimen in account B')
            service.update(matter.matter_id, ACTOR, record['assertion_id'], expected_revision=2,
                record_type='event', title=record['title'], statement=record['statement'], raw_date=record['raw_date'],
                date_uncertainty=record['date_uncertainty'], status='disputed')
            expected = service.export(matter.matter_id, ACTOR, record['assertion_id'])
            assert all(row['available'] for row in expected['accounts'])
            assert bench.workspace.notebook_item(matter.matter_id, ACTOR, seed['note_id']).body == 'Original independent commentary'
        upgraded = root / 'upgraded-backup'
        upgraded_manifest = stopped_copy(runtime, upgraded)
        restored_path = root / 'clean-restored-runtime'
        stopped_copy(upgraded, restored_path)
        # Make both former paths unavailable to rule out accidental live reads.
        runtime.rename(root / 'former-working-runtime-offline')
        upgraded.rename(root / 'upgraded-backup-offline')
        with TestClient(create_workbench_app(restored_path, auth_mode='test')) as client:
            bench = client.app.state.workbench
            matter = bench.matter(seed['slug'], ACTOR)
            assert bench.assertion_service(matter).export(matter.matter_id, ACTOR, record['assertion_id']) == expected
            assert bench.entity_service(matter).export(matter.matter_id, ACTOR, entity['entity_id']) == seed['entity']
            assert bench.workspace.notebook_item(matter.matter_id, ACTOR, seed['note_id']).body == 'Original independent commentary'
            for token in seed['tokens']:
                response = client.get('/matters/' + matter.slug + '?support=' + token)
                assert response.status_code == 200 and 'synthetic parcel' in response.text
            sources = bench.source_store(matter)
            for document in sources.documents.values():
                response = client.get('/matters/' + matter.slug + '/sources/' + sources.action_token(document) + '/content')
                assert response.status_code == 200 and b'synthetic parcel' in response.content
            assert bench.workspace.connection.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
            assert not bench.workspace.connection.execute('PRAGMA foreign_key_check').fetchall()
            # Verified stopped backups remain separate. Explicitly delete only
            # this synthetic restored runtime without creating another export.
            def owner_session():
                expires = bench.workspace._timestamp(bench.workspace.current_time() + timedelta(hours=1))
                return bench.workspace.create_session(ACTOR, hashlib.sha256(os.urandom(32)).hexdigest(),
                    'test', expires, expires)
            session = owner_session()
            exit_record = bench.workspace.case_lifecycle.begin_exit(matter.slug, ACTOR,
                mode='delete_without_export', session_id=session.session_id)
            try:
                bench.workspace.case_lifecycle.issue_confirmation(matter.slug, ACTOR,
                    session.session_id, mode='delete_without_export')
            except WorkspaceProblem as exc:
                assert 'seven-day' in str(exc)
            else:
                raise AssertionError('Synthetic deletion was admitted before the cancellation window ended')
            after_window = datetime.fromisoformat(exit_record.eligible_after.replace('Z', '+00:00')) + timedelta(seconds=1)
            bench.workspace._clock = lambda: after_window
            session = owner_session()
            confirmation = bench.workspace.case_lifecycle.issue_confirmation(matter.slug, ACTOR,
                session.session_id, mode='delete_without_export')
            _, lifecycle = bench.begin_matter_purge(matter.slug, ACTOR, matter.display_name,
                mode='delete_without_export', session_id=session.session_id,
                token=confirmation.token, no_backup_acknowledged=True)
            bench.execute_matter_purge(matter, lifecycle)
            assert bench.workspace.matter_lifecycle(matter.matter_id).state == 'deleted'
            assert all(bench.workspace.connection.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0] == 0 for table in TABLES)
        rollback = root / 'clean-preupgrade-rollback'
        stopped_copy(preupgrade, rollback)
        assert manifest(rollback) == preupgrade_manifest
        subprocess.run([sys.executable, '-c', BASELINE_SCRIPT, str(rollback), str(seed_receipt), 'read'], env=env, check=True)
        print(json.dumps(dict(synthetic=True, baseline=baseline,
            stopped_preupgrade_backup_files=len(preupgrade_manifest), stopped_upgraded_backup_files=len(upgraded_manifest),
            complete_runtime_hashes_verified=True, original_files_and_source_passages_restored=True,
            entities_notes_assertions_accounts_roles_history_preserved=True,
            restored_assertion_purge_verified=True, explicit_no_export_exit=True,
            seven_day_window_enforced=True, matching_baseline_reader_verified_clean_rollback=True,
            integrity='ok', scope='Synthetic application runtime and managed originals; no installed-node or PostgreSQL projection qualification.')))


if __name__ == '__main__':
    main()
