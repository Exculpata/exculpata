#!/usr/bin/env python3
"""Synthetic complete-runtime backup, restored synthesis resume and rollback.

The baseline seeds an actual terminal full-text run. Current code interrupts a
synthesis after durable intermediate work, stops the application, hashes and
copies every runtime file, then resumes from a clean path with former paths
unavailable. Rollback uses the matching pre-upgrade backup and baseline reader.
No installed runtime, model, external service or deployment is used.
"""

from __future__ import annotations

from case_intelligence.legacy_names import (
    LEGACY_STEM,
)

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
BASELINE = '19d57fbec29325ae9b5ab296e6a2adcb18a5e0ec'
ACTOR = 'development-taylor-morgan'

BASELINE_SCRIPT = r'''
from dataclasses import asdict
import hashlib,io,json,sys
from pathlib import Path
from fastapi.testclient import TestClient
from case_intelligence.full_text_review import FullTextReviewLedger,iter_text_export
from case_intelligence.generation import VerifiedReviewDecision,UnavailableGenerator
from case_intelligence.managed_storage import StoragePolicy
from case_intelligence.pilot_uploads import PilotUnit
from case_intelligence.workbench import create_workbench_app
actor='development-taylor-morgan'
runtime,receipt,phase=Path(sys.argv[1]),Path(sys.argv[2]),sys.argv[3]
with TestClient(create_workbench_app(runtime,generator=UnavailableGenerator(),auth_mode='test',storage_policy=StoragePolicy(reserve_bytes=0))) as client:
    bench=client.app.state.workbench
    bench.research.close();bench.full_review.close()
    if phase=='seed':
        matter=bench.create_matter('Synthetic synthesis recovery','Generated originals only',actor)
        statements=[f'Synthetic dispatch record {number} states the crate was '+('not delivered.' if number==24 else 'delivered.') for number in range(1,25)]
        original='\n'.join(statements).encode()
        store=bench.source_store(matter)
        document,_=store.store_stream('synthetic-recovery-dispatch.txt','text/plain',io.BytesIO(original))
        document.units=[asdict(PilotUnit(number,text,excerpt_digest=hashlib.sha256(text.encode()).hexdigest())) for number,text in enumerate(statements,1)]
        store._save([document.document_id]);bench._sync_source_catalog(matter,[document])
        _,criterion=bench.workspace.create_review_criterion(matter.matter_id,actor,title='Synthetic delivery accounts',instructions='Summarize delivery accounts, preserving each record number and conflicting accounts.')
        bench.workspace.queue_review_run(matter.matter_id,actor,criterion.criterion_version_id,run_kind='full',review_mode='full_text')
        run=bench.workspace.claim_review_run('synthetic-baseline-worker')
        bench.generator.classify_source=lambda **kwargs:VerifiedReviewDecision('include',kwargs['evidence'][0].excerpt,('S1',),True,1)
        while (decision:=bench.workspace.next_review_decision(run.run_id)) is not None:
            outcome=bench._process_review_decision(run,decision,lambda:False)
            bench._record_review_decision(run,decision,outcome)
        run=bench._finish_review_run(run)
        assert run.state=='succeeded'
        data=dict(slug=matter.slug,matter_id=matter.matter_id,run_id=run.run_id,document_id=document.document_id,original_sha256=hashlib.sha256(original).hexdigest(),statements=statements)
        data['ledger']=json.loads(b''.join(iter_text_export(bench.workspace,matter.matter_id,actor,run.run_id)))
        receipt.write_text(json.dumps(data))
    else:
        data=json.loads(receipt.read_text())
        matter=bench.matter(data['slug'],actor)
        store=bench.source_store(matter);document=store.get(data['document_id'])
        assert json.loads(b''.join(iter_text_export(bench.workspace,matter.matter_id,actor,data['run_id'])))==data['ledger']
        assert not bench.workspace.research_jobs(matter.matter_id,actor)
        response=client.get('/matters/'+matter.slug+'/sources/'+store.action_token(document)+'/content')
        assert response.status_code==200 and hashlib.sha256(response.content).hexdigest()==data['original_sha256']
    assert bench.workspace.connection.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
    assert not bench.workspace.connection.execute('PRAGMA foreign_key_check').fetchall()
'''


def manifest(directory):
    result = {}
    for path in sorted(directory.rglob('*')):
        if path.is_symlink():
            raise RuntimeError('Synthetic backup unexpectedly contains a symlink.')
        if path.is_file():
            with path.open('rb') as source:
                result[str(path.relative_to(directory))] = hashlib.file_digest(source, 'sha256').hexdigest()
    return result


def stopped_copy(source, destination):
    expected = manifest(source)
    if destination.exists():
        raise RuntimeError('A clean restore target must be absent.')
    shutil.copytree(source, destination)
    assert manifest(destination) == expected
    return expected


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline-ref', default=BASELINE)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if args.output and args.output.exists():
        parser.error('Use a new receipt path; previous results are not overwritten.')
    baseline = subprocess.check_output(['git', 'rev-parse', args.baseline_ref + '^{commit}'], cwd=ROOT, text=True).strip()
    from synthetic_browser_environment import isolate_environment
    isolate_environment()
    sys.path.insert(0, str(ROOT / 'src'))
    from fastapi.testclient import TestClient
    from case_intelligence.managed_storage import StoragePolicy
    from case_intelligence.workbench import create_workbench_app
    spec = importlib.util.spec_from_file_location('full_text_restore_fixture', ROOT / 'scripts/evaluate-full-text-synthesis.py')
    fixtures = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixtures)
    with tempfile.TemporaryDirectory(prefix=f"{LEGACY_STEM}-full-text-restore-") as temporary:
        root = Path(temporary).resolve()
        archive_path = root / 'baseline.tar'
        with archive_path.open('wb') as output:
            subprocess.run(['git', 'archive', baseline, 'src'], cwd=ROOT, stdout=output, check=True)
        with tarfile.open(archive_path) as archive:
            archive.extractall(root / 'baseline', filter='data')
        baseline_env = {**os.environ, 'PYTHONPATH': str(root / 'baseline/src'),
                        'CASE_INTELLIGENCE_STORAGE_RESERVE_GIB': '0'}
        runtime, seed_path = root / 'working-runtime', root / 'seed.json'
        subprocess.run([sys.executable, '-c', BASELINE_SCRIPT, str(runtime), str(seed_path), 'seed'],
            env=baseline_env, cwd=root, check=True)
        seed = json.loads(seed_path.read_text())
        before = stopped_copy(runtime, root / 'preupgrade-backup')
        # Preserve real coordinator recovery but control dispatch timing so
        # the interrupted checkpoint can be examined before the next claim.
        with patch('case_intelligence.workflow_jobs.ResearchCoordinator._run', lambda *a: None):
            with TestClient(create_workbench_app(runtime, generator=fixtures.SourceEcho(), auth_mode='test',
                    storage_policy=StoragePolicy(reserve_bytes=0))) as client:
                bench = client.app.state.workbench
                matter = bench.matter(seed['slug'], ACTOR)
                job, _ = bench.queue_full_text_synthesis(matter, ACTOR, seed['run_id'], 'research-request-' + 'd' * 32)
                claimed = bench.workspace.claim_research_job('synthetic-upgrade-worker')
                original_checkpoint = bench.workspace.checkpoint_research_job
                def checkpoint(*arguments, **keywords):
                    saved = original_checkpoint(*arguments, **keywords)
                    hierarchy = saved.result.get('hierarchical_synthesis', {})
                    if hierarchy.get('requests_spent') == 3 and len(hierarchy.get('issue', [])) == 2:
                        raise RuntimeError('Synthetic interrupted dispatch')
                    return saved
                bench.workspace.checkpoint_research_job = checkpoint
                try:
                    bench._process_research_job(claimed, lambda: False)
                except RuntimeError as exc:
                    assert str(exc) == 'Synthetic interrupted dispatch'
                else:
                    raise AssertionError('The synthetic checkpoint was not interrupted.')
                saved = bench.workspace.research_job(matter.matter_id, ACTOR, job.job_id)
                saved_nodes = saved.result['hierarchical_synthesis']['issue']
                saved_receipt = saved.result['full_text_synthesis_input']
                assert len(saved_nodes) == 2 and saved.result['hierarchical_synthesis']['requests_spent'] == 3
        after = stopped_copy(runtime, root / 'upgraded-backup')
        restored = root / 'clean-restored-runtime'
        stopped_copy(root / 'upgraded-backup', restored)
        runtime.rename(root / 'former-runtime-unavailable')
        (root / 'upgraded-backup').rename(root / 'former-upgraded-backup-unavailable')
        assert not runtime.exists() and not (root / 'upgraded-backup').exists()
        with patch('case_intelligence.workflow_jobs.ResearchCoordinator._run', lambda *a: None):
            with TestClient(create_workbench_app(restored, generator=fixtures.SourceEcho(), auth_mode='test',
                    storage_policy=StoragePolicy(reserve_bytes=0))) as client:
                bench = client.app.state.workbench
                matter = bench.matter(seed['slug'], ACTOR)
                recovered = bench.workspace.research_job(matter.matter_id, ACTOR, job.job_id)
                assert recovered.state == 'queued'
                resumed = bench.workspace.claim_research_job('synthetic-clean-restored-worker')
                result = bench._process_research_job(resumed, lambda: False)
                completed = bench._finish_research_job(resumed, result)
                assert result['hierarchical_synthesis']['issue'][:2] == saved_nodes
                assert result['hierarchical_synthesis']['requests_spent'] == 13
                assert result['full_text_synthesis_input'] == saved_receipt
                assert {claim['text'] for claim in result['answer']['claims']} == set(seed['statements'])
                export_digests = {}
                for format_name in ('json', 'markdown', 'docx'):
                    export = bench.export_research_work_product(matter, completed, format_name)
                    assert export.body
                    export_digests[format_name] = hashlib.sha256(export.body).hexdigest()
                store = bench.source_store(matter)
                document = store.get(seed['document_id'])
                response = client.get(f'/matters/{matter.slug}/sources/{store.action_token(document)}/content')
                assert response.status_code == 200
                assert hashlib.sha256(response.content).hexdigest() == seed['original_sha256']
                for evidence in result['evidence']:
                    response = client.get(f'/matters/{matter.slug}?support={evidence["support_token"]}')
                    assert response.status_code == 200
                    assert evidence['excerpt'] in response.text
                assert bench.workspace.connection.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
                assert not bench.workspace.connection.execute('PRAGMA foreign_key_check').fetchall()
        rollback = root / 'clean-preupgrade-rollback'
        stopped_copy(root / 'preupgrade-backup', rollback)
        assert manifest(rollback) == before
        subprocess.run([sys.executable, '-c', BASELINE_SCRIPT, str(rollback), str(seed_path), 'read'],
            env=baseline_env, cwd=root, check=True)
        receipt = dict(synthetic=True, baseline=baseline, complete_runtime_hashes_verified=True,
            preupgrade_files=len(before), upgraded_files=len(after),
            restored_original_bytes_and_24_source_passages=True,
            saved_intermediate_nodes_reused=2, generation_requests_spent=13,
            input_receipt_unchanged=True, export_sha256=export_digests,
            matching_preupgrade_backup_read_by_baseline=True,
            integrity='ok', foreign_keys='ok',
            scope='Complete stopped synthetic application runtime; no installed-node or PostgreSQL deployment qualification.')
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(receipt, indent=2) + '\n')
        print(json.dumps(receipt))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
