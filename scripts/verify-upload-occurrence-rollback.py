#!/usr/bin/env python3
"""Stopped previous-reader and forward-retry check using generated source bytes."""

from __future__ import annotations

from case_intelligence.legacy_names import (
    LEGACY_STEM,
)

import argparse
import json
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from case_intelligence.pilot_uploads import PilotStore  # noqa: E402


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--previous-source', type=Path, required=True)
    args = parser.parse_args()
    previous = args.previous_source.resolve()
    if not (previous / 'src/case_intelligence/pilot_uploads.py').is_file():
        raise SystemExit('Choose the previous source checkout.')
    with tempfile.TemporaryDirectory(prefix=f"{LEGACY_STEM}-occurrence-reader-") as temporary:
        root = Path(temporary)
        store = PilotStore(root / 'sources')
        body = b'Generated repeated occurrence reader fixture.\n'
        items = ['upload-item-' + character * 32 for character in ('a', 'b')]
        expected = []
        for item in items:
            store.append_resumable_chunk(item, offset=0, expected_size=len(body), chunk=body)
            document = store.finalize_resumable_upload(item, display_name='report.txt',
                relative_path='Records/report.txt', content_type='text/plain', expected_size=len(body))
            expected.append([document.document_id, document.version_id, document.name_key, document.relative_path])
        assert expected[0][0] != expected[1][0]
        store.close()
        contract = root / 'expected.json'
        contract.write_text(json.dumps(expected))
        code = '''
import json,sys
from pathlib import Path
sys.path.insert(0, str(Path(sys.argv[1]) / 'src'))
from case_intelligence.pilot_uploads import PilotStore
root=Path(sys.argv[2]); expected=json.loads((root/'expected.json').read_text())
store=PilotStore(root/'sources')
try:
    assert len(store.documents)==2
    for identity,version,key,path in expected:
        item=store.get(identity)
        assert [item.document_id,item.version_id,item.name_key,item.relative_path]==[identity,version,key,path]
        assert store.source_path(identity).read_bytes()==b'Generated repeated occurrence reader fixture.\\n'
finally:
    store.close()
print('Previous source reader preserves both occurrences and their source identities.')
'''
        subprocess.run([sys.executable, '-c', code, str(previous), str(root)], check=True)
        current = PilotStore(root / 'sources')
        try:
            for item, saved in zip(items, expected):
                document = current.finalize_resumable_upload(item, display_name='report.txt',
                    relative_path='Records/report.txt', content_type='text/plain', expected_size=len(body))
                assert [document.document_id, document.version_id, document.name_key, document.relative_path] == saved
            assert len(current.documents) == 2
        finally:
            current.close()
        print('Forward retry preserves both pending items without creating another source.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
