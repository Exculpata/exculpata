#!/usr/bin/env python3
"""Content-free slice-16 -> slice-17 migration and clean rollback receipt."""

from case_intelligence.legacy_names import (
    LEGACY_STEM,
)
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parents[1]
BASELINE = '3f96b290ca71adbaef37b92e2d9f4d01cf7d226f'
with tempfile.TemporaryDirectory(prefix=f"{LEGACY_STEM}-discovery-migration-") as temporary:
    root = Path(temporary)
    archive_path = root / 'baseline.tar'
    with archive_path.open('wb') as output:
        subprocess.run(['git', 'archive', BASELINE, 'src'], cwd=ROOT, stdout=output, check=True)
    with tarfile.open(archive_path) as archive:
        archive.extractall(root / 'baseline', filter='data')
    env = {**os.environ, 'PYTHONPATH': str(root / 'baseline/src')}
    database = root / 'working.sqlite'
    create = '''from pathlib import Path
from contextlib import nullcontext
import json,sys
from case_intelligence.workspace_store import WorkspaceStore
from case_intelligence.entity_service import EntityService
s=WorkspaceStore(Path(sys.argv[1])); a=s.upsert_principal('test','synthetic-reviewer','Synthetic reviewer','synthetic-reviewer',preferred_principal_id='synthetic-reviewer'); m=s.create_matter('Synthetic migration','',a.principal_id)
reference=dict(document_id='a'*32,source_version_id='b'*32,source_name='Synthetic original.txt',location='Unit 1',unit_number=1,chunk_id='chunk-1',excerpt_digest='c'*64,excerpt='Alex Example arrived.',support_token='d'*40)
e=EntityService(s.entity_repository(),source_guard=nullcontext,resolve_support=lambda token:reference,load_note=s.notebook_item,load_references=s.notebook_references,validate_references=lambda refs:frozenset())
r=e.create(m.matter_id,a.principal_id,display_name='Alex Example',aliases='A. Example',support='d'*40); print(json.dumps([m.matter_id,r['entity_id'],e.detail(m.matter_id,a.principal_id,r['entity_id'])])); s.close()
'''
    result = subprocess.run([sys.executable, '-c', create, str(database)], env=env, capture_output=True, text=True, check=True)
    matter_id, entity_id, expected = json.loads(result.stdout)
    preupgrade = root / 'preupgrade.sqlite'
    with sqlite3.connect(database) as source, sqlite3.connect(preupgrade) as target:
        source.backup(target)
    sys.path.insert(0, str(ROOT / 'src'))
    from case_intelligence.workspace_store import WorkspaceStore
    store = WorkspaceStore(database)
    actual = store.entity_repository().get(matter_id, entity_id)
    assert {key: actual[key] for key in expected[0]} == expected[0]
    assert store.entity_repository().history(matter_id, entity_id) == expected[2]
    mention = store.entity_repository().mentions(matter_id, entity_id)[0]
    assert all(mention[key] == value for key, value in expected[1][0].items() if key != 'available')
    assert store.connection.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    assert not store.connection.execute('PRAGMA foreign_key_check').fetchall()
    store.close()
    rollback = root / 'clean-rollback.sqlite'
    with sqlite3.connect(preupgrade) as source, sqlite3.connect(rollback) as target:
        source.backup(target)
    read = '''from pathlib import Path
import json,sys
from case_intelligence.workspace_store import WorkspaceStore
s=WorkspaceStore(Path(sys.argv[1])); r=s.entity_repository(); expected=json.loads(sys.argv[4]); assert r.get(sys.argv[2],sys.argv[3])==expected[0]; assert r.history(sys.argv[2],sys.argv[3])==expected[2]; assert r.mentions(sys.argv[2],sys.argv[3])==[{key:value for key,value in row.items() if key!='available'} for row in expected[1]]; assert 'extractor_version' not in {r[1] for r in s.connection.execute('PRAGMA table_info(workbench_entity)')}; assert s.connection.execute('PRAGMA integrity_check').fetchone()[0]=='ok'; assert not s.connection.execute('PRAGMA foreign_key_check').fetchall(); s.close()
'''
    subprocess.run([sys.executable, '-c', read, str(rollback), matter_id, entity_id, json.dumps(expected)], env=env, check=True)
    print(json.dumps(dict(synthetic=True, baseline=BASELINE, migration_preserved_entity_mentions_and_history=True,
        clean_preupgrade_rollback_verified_by_original_reader=True,
        full_source_restore='Separately exercised by test_web_discovery_full_source_restore_export_and_purge.')))
