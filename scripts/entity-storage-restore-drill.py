#!/usr/bin/env python3
"""Synthetic slice-16 upgrade, clean restore and baseline rollback; no installed node."""

from case_intelligence.legacy_names import (
    LEGACY_STEM,
)
import argparse
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser()
parser.add_argument('--baseline-ref', default='e11ab25ccbbde7e864914b3bb7ba56603792b8b4')
args = parser.parse_args()
baseline = subprocess.check_output(['git', 'rev-parse', args.baseline_ref + '^{commit}'], cwd=ROOT, text=True).strip()
with tempfile.TemporaryDirectory(prefix=f"{LEGACY_STEM}-entity-restore-") as temporary:
    root = Path(temporary).resolve()
    bundle = root / 'baseline.tar'
    with bundle.open('wb') as output:
        subprocess.run(['git', 'archive', baseline, 'src'], cwd=ROOT, stdout=output, check=True)
    with tarfile.open(bundle) as archive:
        archive.extractall(root / 'baseline', filter='data')
    env = {**os.environ, 'PYTHONPATH': str(root / 'baseline/src')}
    database = root / 'working.sqlite'
    create = '''from pathlib import Path
from case_intelligence.workspace_store import WorkspaceStore
import json,sys
s=WorkspaceStore(Path(sys.argv[1])); a=s.upsert_principal('test','synthetic-entity-reviewer','Synthetic reviewer','synthetic-entity-reviewer',preferred_principal_id='synthetic-entity-reviewer'); m=s.create_matter('Synthetic entity restore','Synthetic',a.principal_id); n,_=s.create_notebook_item(m.matter_id,a.principal_id,item_type='person',status='needs_review',title='Alex Example',body='Synthetic original commentary'); print(json.dumps([m.matter_id,n.item_id])); s.close()
'''
    result = subprocess.run([sys.executable, '-c', create, str(database)], env=env, capture_output=True, text=True, check=True)
    matter_id, note_id = json.loads(result.stdout)
    preupgrade = root / 'preupgrade.sqlite'
    with sqlite3.connect(database) as source, sqlite3.connect(preupgrade) as target:
        source.backup(target)
    sys.path.insert(0, str(ROOT / 'src'))
    from case_intelligence.workspace_store import WorkspaceStore
    from case_intelligence.entity_service import EntityService
    from contextlib import nullcontext
    actor = 'synthetic-entity-reviewer'
    store = WorkspaceStore(database)
    assert store.notebook_item(matter_id, actor, note_id).body == 'Synthetic original commentary'
    reference = dict(document_id='document-' + 'a' * 32, source_version_id='source-version-' + 'a' * 32,
        source_name='Generated original.txt', location='Lines 1–1', unit_number=1, chunk_id='chunk-1',
        excerpt_digest='a' * 64, excerpt='Synthetic original passage naming Alex Example.', support_token='a' * 40)
    def service(control):
        return EntityService(control.entity_repository(), source_guard=nullcontext,
            resolve_support=lambda token: reference, load_note=control.notebook_item, load_references=control.notebook_references,
            validate_references=lambda values: frozenset(index for index, value in enumerate(values)
                if all(value[key] == expected for key, expected in reference.items())))
    entity = service(store).create(matter_id, actor, display_name='Alex Example', support='a' * 40, aliases='A. Example')
    service(store).update(matter_id, actor, entity['entity_id'], expected_revision=1, display_name='Alex Example', status='confirmed')
    expected = service(store).detail(matter_id, actor, entity['entity_id'])
    backup = root / 'upgraded-backup.sqlite'
    with store._lock, sqlite3.connect(backup) as target:
        store.connection.backup(target)
    store.close()
    restored_path = root / 'clean-restore' / 'workspace.sqlite'
    restored_path.parent.mkdir()
    with sqlite3.connect(backup) as source, sqlite3.connect(restored_path) as target:
        source.backup(target)
    restored = WorkspaceStore(restored_path)
    assert service(restored).detail(matter_id, actor, entity['entity_id']) == expected
    assert restored.notebook_item(matter_id, actor, note_id).body == 'Synthetic original commentary'
    assert restored.connection.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    assert not restored.connection.execute('PRAGMA foreign_key_check').fetchall()
    restored.close()
    rollback = root / 'clean-rollback.sqlite'
    with sqlite3.connect(preupgrade) as source, sqlite3.connect(rollback) as target:
        source.backup(target)
    read = '''from pathlib import Path
from case_intelligence.workspace_store import WorkspaceStore
import sys
s=WorkspaceStore(Path(sys.argv[1])); assert s.notebook_item(sys.argv[2],'synthetic-entity-reviewer',sys.argv[3]).body=='Synthetic original commentary'; assert not s.connection.execute("SELECT 1 FROM sqlite_master WHERE name='workbench_entity'").fetchone(); assert s.connection.execute('PRAGMA integrity_check').fetchone()[0]=='ok'; assert not s.connection.execute('PRAGMA foreign_key_check').fetchall(); s.close()
'''
    subprocess.run([sys.executable, '-c', read, str(rollback), matter_id, note_id], env=env, check=True)
    print(json.dumps(dict(synthetic=True, baseline=baseline, upgrade_preserved_notes=True,
        clean_restore_preserved_entity_mentions_alias_history=True, baseline_reader_verified_clean_rollback=True,
        integrity='ok', scope='SQLite control store only; original-source restoration is exercised separately.')))
