#!/usr/bin/env python3
"""Synthetic baseline upgrade, clean restore and matching-version rollback."""

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
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--answer-context', action='store_true')
parser.add_argument('--matter-purpose', action='store_true')
args = parser.parse_args()
BASELINE = 'd21c1d4849b46b8ae85db19b4d2ce80c8a9669a8' if args.answer_context else '0d59d5fe4f7be575a66039274415de945a6d5db5'
if args.matter_purpose:
    BASELINE = '7f37c72c2922c983762d621a4573f1aa32078bb2'
ACTOR = 'synthetic-context-reviewer'
with tempfile.TemporaryDirectory(prefix=f"{LEGACY_STEM}-context-upgrade-") as temporary:
    root = Path(temporary)
    archive_path = root/'baseline.tar'
    with archive_path.open('wb') as output:
        subprocess.run(['git','archive',BASELINE,'src'],cwd=ROOT,stdout=output,check=True)
    with tarfile.open(archive_path) as archive:
        archive.extractall(root/'baseline',filter='data')
    env = {**os.environ,'PYTHONPATH':str(root/'baseline/src')}
    database = root/'working.sqlite'
    create = '''from pathlib import Path
from case_intelligence.workspace_store import WorkspaceStore
import json,sys
s=WorkspaceStore(Path(sys.argv[1])); actor='synthetic-context-reviewer'; s.upsert_principal('test',actor,'Synthetic reviewer',actor,preferred_principal_id=actor); m=s.create_matter('Synthetic context migration','',actor); n,_=s.create_notebook_item(m.matter_id,actor,item_type='note',status='confirmed',title='Synthetic retained note',body='Original orientation'); c=s.get_conversation(m.matter_id); j,_=s.queue_answer_job(m.matter_id,c.conversation_id,actor,'Synthetic question','answer-request-'+'b'*32,notebook_mode='confirmed'); print(json.dumps([m.matter_id,n.item_id,j.job_id])); s.close()
'''
    result = subprocess.run([sys.executable,'-c',create,str(database)],env=env,capture_output=True,text=True,check=True)
    matter_id,note_id,job_id = json.loads(result.stdout)
    preupgrade = root/'preupgrade.sqlite'
    with sqlite3.connect(database) as source,sqlite3.connect(preupgrade) as target:
        source.backup(target)
    sys.path.insert(0,str(ROOT/'src'))
    from case_intelligence.workspace_store import WorkspaceStore
    from case_intelligence.matter_context import MatterContextService
    from case_intelligence.entity_service import EntityService
    from case_intelligence.assertion_service import AssertionService
    from contextlib import nullcontext
    def service(store):
        entities = EntityService(store.entity_repository(),source_guard=nullcontext,
            resolve_support=lambda _: None,load_note=store.notebook_item,load_references=store.notebook_references,
            validate_references=lambda _: frozenset())
        return MatterContextService(AssertionService(store.assertion_repository(),entities))
    store = WorkspaceStore(database)
    assert store.answer_notebook_context(matter_id,job_id)[1][0].body == 'Original orientation'
    context = service(store)
    selection,candidate = context.inspect(matter_id,ACTOR,kind='notebook_item',object_id=note_id)
    context.change(matter_id,ACTOR,expected_revision=0,action='add',kind='notebook_item',object_id=note_id,approval=candidate['approval'])
    expected = context.repository.export(matter_id)
    def purpose_bytes(instance):
        record = dict(instance.connection.execute('SELECT * FROM workbench_matter_purpose WHERE matter_id=?', (matter_id,)).fetchone())
        audit = [dict(row) for row in instance.connection.execute(
            "SELECT * FROM workbench_audit_event WHERE matter_id=? AND action='matter.purpose_update' ORDER BY occurred_at,event_id", (matter_id,))]
        return json.dumps(dict(record=record,audit=audit),sort_keys=True,ensure_ascii=False).encode('utf-8')
    if args.matter_purpose:
        assert store.matter_purpose(matter_id, ACTOR)['revision'] == 0
        store.update_matter_purpose(matter_id, ACTOR, expected_revision=0,
            objective='  Synthetic objective — café  ', background='Synthetic background\r\nKeep e\u0301 and 終.',
            request_id='synthetic-purpose-restore')
        expected_purpose = purpose_bytes(store)
    if args.answer_context:
        from case_intelligence.answer_context import AnswerContextRepository, freeze
        answer_job, _ = store.queue_answer_job(matter_id, None, ACTOR, 'Synthetic recorded context',
            'answer-request-'+'c'*32, use_saved_context=True,
            snapshot_builder=lambda: freeze(context,matter_id,ACTOR,1,None,None))
        # Storage fixture: one prepared input; no dispatch is claimed.
        claimed = store.claim_answer_job('synthetic-storage-worker')
        if claimed.job_id != answer_job.job_id:
            store.fail_answer_job(claimed.job_id,'Synthetic old job stopped')
            claimed = store.claim_answer_job('synthetic-storage-worker')
        repo = AnswerContextRepository(store)
        repo.prepare(claimed,dict(format='synthetic-storage-preparation',request={'messages':[]}))
        expected_answer = repo.receipt(matter_id,answer_job.job_id)
    upgraded = root/'upgraded-backup.sqlite'
    with sqlite3.connect(upgraded) as target:
        store.connection.backup(target)
    store.close()
    restored_path = root/'clean-restore'/'workspace.sqlite'
    restored_path.parent.mkdir()
    with sqlite3.connect(upgraded) as source,sqlite3.connect(restored_path) as target:
        source.backup(target)
    restored = WorkspaceStore(restored_path)
    assert service(restored).repository.export(matter_id) == expected
    assert service(restored).inspect(matter_id,ACTOR)[0]['rows'][0]['state'] == 'Unchanged'
    assert restored.answer_notebook_context(matter_id,job_id)[1][0].body == 'Original orientation'
    assert restored.connection.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    assert not restored.connection.execute('PRAGMA foreign_key_check').fetchall()
    if args.answer_context:
        assert AnswerContextRepository(restored).receipt(matter_id,answer_job.job_id) == expected_answer
    if args.matter_purpose:
        assert purpose_bytes(restored) == expected_purpose
    restored.close()
    if args.matter_purpose:
        restored = WorkspaceStore(restored_path)
        assert purpose_bytes(restored) == expected_purpose
        restored.close()
    verify = '''from pathlib import Path
from case_intelligence.workspace_store import WorkspaceStore
import sys
s=WorkspaceStore(Path(sys.argv[1])); assert s.answer_notebook_context(sys.argv[2],sys.argv[3])[1][0].body=='Original orientation'; assert s.connection.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='workbench_context_selection'").fetchone()[0]==0; s.close()
'''
    if args.answer_context:
        verify = verify.replace("name='workbench_context_selection'", "name='workbench_answer_context'")
    if args.matter_purpose:
        verify = verify.replace("name='workbench_context_selection'", "name='workbench_matter_purpose'")
    subprocess.run([sys.executable,'-c',verify,str(preupgrade),matter_id,job_id],env=env,check=True)
    print(json.dumps(dict(synthetic_only=True,baseline=BASELINE,upgrade=True,clean_restore=True,
                         historical_answer_scope=True,answer_context=args.answer_context,
                         matter_purpose=args.matter_purpose,purpose_and_audit_byte_identical=args.matter_purpose,matching_preupgrade_rollback=True,mixed_version_writers=False)))
