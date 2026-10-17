#!/usr/bin/env python3
"""Synthetic-only SQLite upgrade/backup rollback evidence; never opens an installed node."""

from case_intelligence.legacy_names import (
    LEGACY_STEM,
)
import argparse, json, os, pathlib, sqlite3, subprocess, sys, tarfile, tempfile
repo=pathlib.Path(__file__).resolve().parents[1]
parser=argparse.ArgumentParser(description="Verify synthetic Report migration and pre-upgrade backup rollback with the baseline reader.")
parser.add_argument('--baseline-ref', default='1fed77a2c50686b073e4c3255421d77aa16fefe5')
args=parser.parse_args()
baseline=subprocess.check_output(['git','rev-parse',args.baseline_ref + '^{commit}'],cwd=repo,text=True).strip()
with tempfile.TemporaryDirectory(prefix=f"{LEGACY_STEM}-report-rollback-") as directory:
    root=pathlib.Path(directory)
    bundle=root/'baseline.tar'
    subprocess.run(['git','archive',baseline,'src'],cwd=repo,stdout=bundle.open('wb'),check=True)
    with tarfile.open(bundle) as archive: archive.extractall(root/'baseline',filter='data')
    database=root/'legacy.sqlite'
    create='''from case_intelligence.workspace_store import WorkspaceStore
import sys,json
s=WorkspaceStore(sys.argv[1]); a=s.upsert_principal('test','synthetic-rollback','Synthetic reviewer','synthetic-rollback',preferred_principal_id='synthetic-rollback'); m=s.create_matter('Synthetic rollback','Synthetic',a.principal_id); r=s.create_report_from_sections(m.matter_id,a.principal_id,'Synthetic retained report','',origin_id='synthetic-rollback',sections=[{'heading':'Human note','body':'Synthetic original body.'}]); print(json.dumps([m.matter_id,r.report_id])); s.close()
'''
    env={**os.environ,'PYTHONPATH':str(root/'baseline/src')}
    result=subprocess.run([sys.executable,'-c',create,str(database)],env=env,text=True,capture_output=True,check=True)
    matter,report=json.loads(result.stdout)
    backup=root/'preupgrade-backup.sqlite'
    with sqlite3.connect(database) as source, sqlite3.connect(backup) as target: source.backup(target)
    sys.path.insert(0,str(repo/'src'))
    from case_intelligence.workspace_store import WorkspaceStore
    store=WorkspaceStore(database)
    assert store.report_sections(matter,report)[0].body=='Synthetic original body.'
    assert 'compilation_basis' in {row[1] for row in store.connection.execute('PRAGMA table_info(workbench_report_section)')}
    assert store.connection.execute("SELECT 1 FROM sqlite_master WHERE name='workbench_report_compilation_job'").fetchone()
    assert store.connection.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
    store.close()
    restored=root/'clean-rollback.sqlite'
    with sqlite3.connect(backup) as source, sqlite3.connect(restored) as target: source.backup(target)
    read='''from case_intelligence.workspace_store import WorkspaceStore
import sys
s=WorkspaceStore(sys.argv[1]); section=s.report_sections(sys.argv[2],sys.argv[3])[0]; assert section.body=='Synthetic original body.'; assert s.connection.execute('PRAGMA integrity_check').fetchone()[0]=='ok'; assert not s.connection.execute("SELECT 1 FROM sqlite_master WHERE name='workbench_report_compilation_job'").fetchone(); s.close(); print('baseline reader verified pre-upgrade backup restore')
'''
    final=subprocess.run([sys.executable,'-c',read,str(restored),matter,report],env=env,text=True,capture_output=True,check=True)
    print(json.dumps({'baseline':baseline,'synthetic':True,'upgrade_preserved_existing_report':True,'clean_rollback_restore_verified_by_original_code':True,'integrity':'ok','result':final.stdout.strip()}))
