#!/usr/bin/env python3
"""Content-free migration 0037 upgrade, discovery, backup and clean rollback receipt.

The baseline is the last accepted revision before the automatic discovery
ledger. A synthetic store created by that revision is backed up, upgraded by
this checkout, discovered automatically, backed up again and restored cleanly.
The untouched pre-upgrade backup is then opened by the baseline reader.
"""

from case_intelligence.legacy_names import (
    LEGACY_STEM,
)
import json
import os
from contextlib import nullcontext
from pathlib import Path
import sqlite3
import subprocess
import sys
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parents[1]
BASELINE = 'd9914114b4488bec6fe9f212320a13e95a908722'

CREATE = '''from pathlib import Path
from contextlib import nullcontext
import json,sys
from case_intelligence.workspace_store import WorkspaceStore
from case_intelligence.entity_service import EntityService
s=WorkspaceStore(Path(sys.argv[1])); a=s.upsert_principal('test','synthetic-reviewer','Synthetic reviewer','synthetic-reviewer',preferred_principal_id='synthetic-reviewer'); m=s.create_matter('Synthetic migration','',a.principal_id)
reference=dict(document_id='a'*32,source_version_id='b'*32,source_name='Synthetic original.txt',location='Unit 1',unit_number=1,chunk_id='chunk-1',excerpt_digest='c'*64,excerpt='Alex Example arrived.',support_token='d'*40)
e=EntityService(s.entity_repository(),source_guard=nullcontext,resolve_support=lambda token:reference,load_note=s.notebook_item,load_references=s.notebook_references,validate_references=lambda refs:frozenset())
r=e.create(m.matter_id,a.principal_id,display_name='Alex Example',aliases='A. Example',support='d'*40); print(json.dumps([m.matter_id,r['entity_id'],e.detail(m.matter_id,a.principal_id,r['entity_id'])])); s.close()
'''

READ = '''from pathlib import Path
import json,sys
from case_intelligence.workspace_store import WorkspaceStore
s=WorkspaceStore(Path(sys.argv[1])); r=s.entity_repository(); expected=json.loads(sys.argv[4])
assert r.get(sys.argv[2],sys.argv[3])==expected[0]; assert r.history(sys.argv[2],sys.argv[3])==expected[2]
assert not s.connection.execute("SELECT name FROM sqlite_master WHERE name='workbench_entity_auto_discovery_unit'").fetchall()
assert s.connection.execute('PRAGMA integrity_check').fetchone()[0]=='ok'; assert not s.connection.execute('PRAGMA foreign_key_check').fetchall(); s.close()
'''


def copy(source, target):
    with sqlite3.connect(source) as left, sqlite3.connect(target) as right:
        left.backup(right)


def main():
    with tempfile.TemporaryDirectory(prefix=f"{LEGACY_STEM}-automatic-discovery-") as temporary:
        root = Path(temporary)
        archive_path = root / 'baseline.tar'
        with archive_path.open('wb') as output:
            subprocess.run(['git', 'archive', BASELINE, 'src'], cwd=ROOT, stdout=output, check=True)
        with tarfile.open(archive_path) as archive:
            archive.extractall(root / 'baseline', filter='data')
        env = {**os.environ, 'PYTHONPATH': str(root / 'baseline/src')}
        database = root / 'working.sqlite'
        result = subprocess.run([sys.executable, '-c', CREATE, str(database)], env=env,
                                capture_output=True, text=True, check=True)
        matter_id, entity_id, expected = json.loads(result.stdout)
        preupgrade = root / 'preupgrade.sqlite'
        copy(database, preupgrade)

        sys.path.insert(0, str(ROOT / 'src'))
        from case_intelligence.entity_discovery import EntityDiscovery
        from case_intelligence.entity_service import EntityService
        from case_intelligence.workspace_store import AUTOMATIC_DISCOVERY_PRINCIPAL, WorkspaceStore

        store = WorkspaceStore(database)
        repository = store.entity_repository()
        actual = repository.get(matter_id, entity_id)
        assert {key: actual[key] for key in expected[0]} == expected[0]
        assert repository.history(matter_id, entity_id) == expected[2]
        principal_sql = 'SELECT active FROM workbench_principal WHERE principal_id=?'
        # The upgrade adds no principal; automatic discovery creates its own on first use.
        assert store.connection.execute(principal_sql, (AUTOMATIC_DISCOVERY_PRINCIPAL,)).fetchone() is None

        # Discover one synthetic ready source as the system principal.
        text = 'Jordan Sample met Riley Placeholder on 03/04/2026.'
        document_id, version_id = 'e' * 32, 'f' * 32
        store.upsert_source_catalog(matter_id, (dict(
            document_id=document_id, version_id=version_id, action_token='4' * 32,
            display_name='Synthetic memo.txt', relative_path='Synthetic memo.txt', media_type='text/plain',
            kind='TXT', source_state='ready', tone='ready', state_label='Ready', count_label='1 unit',
            processing_stage='', completed_units=1, total_units=1, page_count=0, duration_ms=0,
            byte_size=len(text), source_sha256='2' * 64, origin='upload', retryable=False, removable=True,
            has_video=False, content_basis_digest='3' * 64),))

        def load_document(document, version, ordinals=None):
            assert (document, version) == (document_id, version_id)
            yield 1, text, dict(document_id=document_id, source_version_id=version_id,
                                source_name='Synthetic memo.txt', location='Unit 1', unit_number=1,
                                chunk_id='chunk-1', excerpt_digest='0' * 64, excerpt=text, support_token='1' * 40)

        service = EntityService(store.entity_repository(automatic=True), source_guard=nullcontext,
                                resolve_support=lambda token: None, load_note=store.notebook_item,
                                load_references=store.notebook_references,
                                validate_references=lambda refs: frozenset(range(len(refs))))
        discovery = EntityDiscovery(service, load_document=load_document)
        assert discovery.automatic_step(matter_id) == (1, 1, 0)
        assert discovery.automatic_step(matter_id) == (0, 0, 0)
        assert store.connection.execute(principal_sql, (AUTOMATIC_DISCOVERY_PRINCIPAL,)).fetchone()[0] == 0
        exported = store.entity_repository().discovery_export(matter_id)
        suggested = sorted(row[0] for row in store.connection.execute(
            "SELECT display_name FROM workbench_entity WHERE matter_id=? AND status='suggested'", (matter_id,)))
        assert suggested == ['03/04/2026', 'Jordan Sample', 'Riley Placeholder'], suggested
        assert store.connection.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        assert not store.connection.execute('PRAGMA foreign_key_check').fetchall()
        store.close()

        # Clean restore of the upgraded backup preserves the ledger and suggestions.
        restored = root / 'restored.sqlite'
        copy(database, restored)
        again = WorkspaceStore(restored)
        assert again.entity_repository().discovery_export(matter_id) == exported
        again.close()

        # Clean rollback: the untouched pre-upgrade backup opens in the baseline reader.
        rollback = root / 'clean-rollback.sqlite'
        copy(preupgrade, rollback)
        subprocess.run([sys.executable, '-c', READ, str(rollback), matter_id, entity_id, json.dumps(expected)],
                       env=env, check=True)
        print(json.dumps(dict(
            synthetic=True, baseline=BASELINE, migration='0037_automatic_entity_discovery',
            upgrade_preserved_entity_and_history=True, system_principal_inactive=True,
            automatic_suggestions=len(suggested), automatic_coverage_rows=len(exported['automatic_coverage']),
            clean_restore_preserved_ledger=True,
            clean_preupgrade_rollback_verified_by_baseline_reader=True)))


if __name__ == '__main__':
    main()
