#!/usr/bin/env python3
"""Synthetic pre-C2 upgrade, clean restore/export/purge and matching-reader rollback."""
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parents[1]
BASELINE = 'f39acc5fd04c250d94aacc66207d5f23cc12ac73'
ACTOR, DOCUMENT, TOKEN = 'synthetic-role-reviewer', 'a' * 32, 'b' * 32
with tempfile.TemporaryDirectory(prefix='exculpata-role-restore-') as temporary:
    root = Path(temporary)
    archive_path = root / 'baseline.tar'
    with archive_path.open('wb') as output:
        subprocess.run(['git', 'archive', BASELINE, 'src'], cwd=ROOT, stdout=output, check=True)
    with tarfile.open(archive_path) as archive:
        archive.extractall(root / 'baseline', filter='data')
    env = {**os.environ, 'PYTHONPATH': str(root / 'baseline/src')}
    database = root / 'working.sqlite'
    create = '''from pathlib import Path
from case_intelligence.workspace_store import WorkspaceStore
import sys
s=WorkspaceStore(Path(sys.argv[1])); actor='synthetic-role-reviewer'; s.upsert_principal('test',actor,'Synthetic reviewer',actor,preferred_principal_id=actor)
m=s.create_matter('Synthetic role migration','Unchanged descriptor',actor)
c=s.create_source_collection(m.matter_id,'Synthetic intake','upload',actor)
s.register_source_organizations(m.matter_id,c.collection_id,[('a'*32,'synthetic-charge.txt')],actor)
s.upsert_source_catalog(m.matter_id,[dict(document_id='a'*32,version_id='c'*32,action_token='b'*32,display_name='synthetic-charge.txt',relative_path='synthetic-charge.txt',media_type='text/plain',kind='TXT',tone='ready',origin='upload',source_state='ready',state_label='Ready',count_label='1 section')])
print(m.matter_id); s.close()
'''
    result = subprocess.run([sys.executable, '-c', create, str(database)], env=env, capture_output=True, text=True, check=True)
    matter_id = result.stdout.strip()
    preupgrade = root / 'preupgrade.sqlite'
    with sqlite3.connect(database) as source, sqlite3.connect(preupgrade) as target:
        source.backup(target)
    sys.path.insert(0, str(ROOT / 'src'))
    from case_intelligence.workspace_store import WorkspaceStore
    from case_intelligence.source_roles import SourceRoleService, add_source_role_export
    store = WorkspaceStore(database)
    service = SourceRoleService(store)
    assert service.read(matter_id, ACTOR, TOKEN)['source_role'] == 'unclassified'
    saved = service.update(matter_id, ACTOR, TOKEN, expected_revision=0, source_role='background', request_id='synthetic-restore-edit')
    def export(instance):
        artifacts = []
        add_source_role_export(instance, matter_id, ACTOR, False, lambda **a: artifacts.append(a))
        return artifacts[0]['artifact'].body
    expected = export(store)
    restored_path = root / 'clean.sqlite'
    with sqlite3.connect(restored_path) as target:
        store.connection.backup(target)
    matter = store.get_matter_by_id(matter_id)
    store.close()
    for _ in range(2):
        restored = WorkspaceStore(restored_path)
        assert SourceRoleService(restored).read(matter_id, ACTOR, TOKEN) == saved
        assert export(restored) == expected
        assert restored.connection.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        assert not restored.connection.execute('PRAGMA foreign_key_check').fetchall()
        restored.close()
    restored = WorkspaceStore(restored_path)
    _, lifecycle = restored.begin_matter_purge(matter.slug, ACTOR, matter.display_name, source_count=1)
    restored.complete_matter_purge(matter_id, lifecycle.purge_id)
    assert not restored.connection.execute('SELECT * FROM workbench_source_organization').fetchall()
    assert len([e for e in restored.audit_events(matter_id) if e.action == 'source.role_update']) == 1
    restored.close()
    verify = '''from pathlib import Path
from case_intelligence.workspace_store import WorkspaceStore
import sys
s=WorkspaceStore(Path(sys.argv[1])); assert 'source_role' not in {r[1] for r in s.connection.execute('PRAGMA table_info(workbench_source_organization)')}; assert len(s.source_organizations(sys.argv[2]))==1; assert s.source_catalog_record(sys.argv[2],'a'*32).version_id=='c'*32; s.close()
'''
    subprocess.run([sys.executable, '-c', verify, str(preupgrade), matter_id], env=env, check=True)
    print(json.dumps(dict(synthetic_only=True, baseline=BASELINE, defaulted_upgrade=True,
        clean_restore_and_reopen=True, export_and_audit_byte_identical=True, purge=True, matching_preupgrade_rollback=True)))
