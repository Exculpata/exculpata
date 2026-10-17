#!/usr/bin/env python3
"""Prove previous-reader compatibility on stopped synthetic receipt/upload state.

This is a read-compatibility check, not permission to operate an older version
without receipt UI/exports. Rollback after receipt use needs the complete verified
pre-upgrade backup or a forward correction.
"""

from __future__ import annotations

from case_intelligence.legacy_names import (
    LEGACY_STEM,
)

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT/'src'), str(ROOT)]
from fastapi.testclient import TestClient  # noqa: E402
from case_intelligence.generation import UnavailableGenerator  # noqa: E402
from case_intelligence.intake_receipts import IntakeReceipts  # noqa: E402
from case_intelligence.workbench import create_workbench_app  # noqa: E402
from case_intelligence.workspace_store import WorkspaceStore  # noqa: E402
from tests.test_intake_receipt_http import ACTOR, descriptor, put, selection, upload  # noqa: E402

OLD_READER = r'''
import json,sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from fastapi.testclient import TestClient
from case_intelligence.generation import UnavailableGenerator
from case_intelligence.workbench import create_workbench_app
app=create_workbench_app(Path(sys.argv[2]), generator=UnavailableGenerator(), auth_mode='test', background_ingestion=False)
with TestClient(app) as client:
    row=json.loads(sys.argv[3])
    assert client.get('/matters/'+row['slug']+'/setup').status_code==200
    session=client.get(row['status_url']).json()
    assert session['items'][0]['received_size']==7
    assert session['upload_session_id']==row['session_id']
    assert app.state.workbench.workspace.connection.execute('PRAGMA foreign_key_check').fetchall()==[]
print('Previous reader: existing source page and saved byte offsets remain readable.')
'''


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--previous-source', type=Path, required=True)
    args = parser.parse_args()
    previous = args.previous_source.resolve()
    if 'intake_receipts' in (previous/'src/case_intelligence/workbench.py').read_text():
        raise SystemExit('Choose a previous checkout without selection receipt support.')
    with tempfile.TemporaryDirectory(prefix=f"{LEGACY_STEM}-receipt-rollback-") as d:
        runtime = Path(d)/'runtime'
        app = create_workbench_app(runtime, generator=UnavailableGenerator(), auth_mode='test', background_ingestion=True)
        body = b'Synthetic source retained during reader compatibility.\n'
        with TestClient(app) as client:
            response = client.post('/matters', data={'name': 'Generated rollback receipt', 'descriptor': ''}, follow_redirects=False)
            slug = response.headers['location'].split('/')[2]
            bench = app.state.workbench
            matter = bench.matter(slug, ACTOR)
            files = [descriptor('Generated/record.txt', len(body)), descriptor('Skipped/opaque.bin', 3)]
            receipt = selection(client, slug, files, [0])
            session = upload(client, slug, receipt, files[:1], [0])
            put(client, session['items'][0], body[:7])
            expected = IntakeReceipts(bench.workspace).snapshot(matter.matter_id, ACTOR, receipt['receipt_id'])
            control = bench.workspace.path
        completed = subprocess.run([sys.executable, '-c', OLD_READER, str(previous/'src'), str(runtime),
            json.dumps({'slug': slug, 'session_id': session['upload_session_id'], 'status_url': session['status_url']})],
            cwd=Path(d), check=True, capture_output=True, text=True, timeout=30)
        print(completed.stdout.strip())
        reopened = WorkspaceStore(control)
        try:
            assert IntakeReceipts(reopened).snapshot(matter.matter_id, ACTOR, receipt['receipt_id']) == expected
        finally:
            reopened.close()
        with TestClient(create_workbench_app(runtime, generator=UnavailableGenerator(), auth_mode='test', background_ingestion=True)) as client:
            resumed = upload(client, slug, receipt, files[:1], [0])
            assert resumed['upload_session_id'] == session['upload_session_id']
            assert resumed['items'][0]['received_size'] == 7
            put(client, resumed['items'][0], body[7:], 7)
            assert client.post(resumed['items'][0]['finalize_url']).status_code == 200
            bench = client.app.state.workbench
            document = next(iter(bench.source_store(matter).documents.values()))
            assert (bench.source_store(matter).files/document.stored_name).read_bytes() == body
        print('Forward reader: complete receipt unchanged; exact upload bytes resume without a duplicate session.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
