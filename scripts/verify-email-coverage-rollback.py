#!/usr/bin/env python3
"""Preserve old email passages across upgrade, stopped rollback and forward read."""

from __future__ import annotations

from case_intelligence.legacy_names import (
    LEGACY_STEM,
)

import argparse
from email.message import EmailMessage
import json
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from fastapi.testclient import TestClient  # noqa: E402
from case_intelligence.extended_extract import EMAIL_COVERAGE_NOTICE  # noqa: E402
from case_intelligence.generation import UnavailableGenerator  # noqa: E402
from case_intelligence.malware_scan import MalwareScanResult  # noqa: E402
from case_intelligence.workbench import create_workbench_app  # noqa: E402

ACTOR = "development-taylor-morgan"


class CleanScanner:
    def scan(self, _path):
        return MalwareScanResult("clean", "synthetic")


class EvidenceEchoGenerator:
    available = True

    def generate(self, **kwargs):
        evidence = kwargs["evidence"]
        return {"answerable": bool(evidence), "claims": [
            {"text": evidence[0].excerpt, "evidence_ids": [evidence[0].evidence_id]}
        ] if evidence else [], "limitation": None,
            "missing_information": "" if evidence else "No matching support."}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--previous-source", type=Path, required=True)
    args = parser.parse_args()
    previous = args.previous_source.resolve()
    assert (previous / "src/case_intelligence/extended_extract.py").is_file()
    with tempfile.TemporaryDirectory(prefix=f"{LEGACY_STEM}-email-reader-") as temporary:
        root = Path(temporary)
        message = EmailMessage()
        message["Subject"] = "Generated older email"
        message.set_content("Generated parent body canary.")
        attached = EmailMessage()
        attached.set_content("Generated legacy attached-body canary.")
        message.add_attachment(attached, filename="forwarded.eml")
        original = message.as_bytes()
        (root / "generated.eml").write_bytes(original)
        seed = '''
import json,sys
from pathlib import Path
sys.path.insert(0,str(Path(sys.argv[1])/'src'))
from fastapi.testclient import TestClient
from case_intelligence.generation import UnavailableGenerator
from case_intelligence.malware_scan import MalwareScanResult
from case_intelligence.workbench import create_workbench_app
class CleanScanner:
    def scan(self,_path): return MalwareScanResult('clean','synthetic')
root=Path(sys.argv[2])
with TestClient(create_workbench_app(root/'runtime',generator=UnavailableGenerator(),auth_mode='test',malware_scanner=CleanScanner())) as client:
    created=client.post('/matters',data={'name':'Generated previous email reader'},follow_redirects=False)
    slug=created.headers['location'].split('/')[2]
    assert client.post('/matters/'+slug+'/uploads',files=[('files',('generated.eml',(root/'generated.eml').read_bytes(),'message/rfc822'))]).status_code==200
    bench=client.app.state.workbench; matter=bench.matter(slug,'development-taylor-morgan')
    store=bench.source_store(matter); document=next(iter(store.documents.values()))
    units=[(u.number,u.location,u.text,u.excerpt_digest) for u in document.parsed_units()]
    assert any('legacy attached-body canary' in u[2] for u in units)
    expected={'slug':slug,'id':document.document_id,'version':document.version_id,'digest':document.digest,'units':units}
    expected['support']=bench._support_token(bench._candidate(matter,document,document.parsed_units()[-1],len(units)))
    (root/'expected.json').write_text(json.dumps(expected))
print('Previous extractor reproduction contains attached-message text in parent units.')
'''
        subprocess.run([sys.executable, "-c", seed, str(previous), str(root)], check=True)
        expected = json.loads((root / "expected.json").read_text())
        with TestClient(create_workbench_app(root / "runtime", generator=EvidenceEchoGenerator(),
                auth_mode="test", malware_scanner=CleanScanner())) as client:
            bench = client.app.state.workbench
            matter = bench.matter(expected["slug"], ACTOR)
            store = bench.source_store(matter)
            old = store.get(expected["id"])
            assert [[u.number, u.location, u.text, u.excerpt_digest] for u in old.parsed_units()] == expected["units"]
            assert [old.version_id, old.digest] == [expected["version"], expected["digest"]]
            resolved, units, index = bench._find_support(matter, expected["support"])
            assert resolved.version_id == expected["version"]
            assert units[index].text == expected["units"][-1][2]
            review = client.get(f"/matters/{matter.slug}/sources/{store.action_token(old)}")
            assert review.status_code == 200 and EMAIL_COVERAGE_NOTICE in review.text
            assert client.post(f"/matters/{matter.slug}/uploads", files=[("files",
                ("new-occurrence.eml", original, "message/rfc822"))]).status_code == 200
            fresh = next(d for d in store.documents.values() if d.document_id != old.document_id)
            assert all("legacy attached-body canary" not in u.text for u in fresh.parsed_units())
            conversation = bench.workspace.get_conversation(matter.matter_id)
            answer = bench.ask(matter, conversation, "What does the generated parent body say?")
            assert answer.payload["source_coverage"]["notice"] == EMAIL_COVERAGE_NOTICE
            expected["conversation"] = conversation.conversation_id
            expected["fresh_id"] = fresh.document_id
            expected["fresh_version"] = fresh.version_id
            expected["fresh_units"] = [[u.number, u.location, u.text, u.excerpt_digest] for u in fresh.parsed_units()]
            # Preserve the optional internal checkpoint field through a stopped
            # older control-store reader; no old/new workers run concurrently.
            bench.research.close()
            job, _ = bench.workspace.queue_research_job(matter.matter_id, ACTOR,
                "What does the generated parent body say?", "Generated checkpoint compatibility",
                "research-request-" + "d" * 32)
            bench.workspace.claim_research_job("generated-checkpoint-worker")
            expected["checkpoint_job"] = job.job_id
            expected["source_fingerprint"] = bench.workspace.source_availability_fingerprint(matter.matter_id)
            bench.workspace.checkpoint_research_job(job.job_id,
                {"passes": [], "evidence": [], "candidate_count": 0,
                    "retrieval_source_fingerprint": expected["source_fingerprint"]})
            expected["matter_id"] = matter.matter_id
            (root / "expected.json").write_text(json.dumps(expected))
        old_reader = '''
import json,sys
from pathlib import Path
sys.path.insert(0,str(Path(sys.argv[1])/'src'))
from fastapi.testclient import TestClient
from case_intelligence.generation import UnavailableGenerator
from case_intelligence.workbench import create_workbench_app
root=Path(sys.argv[2]); expected=json.loads((root/'expected.json').read_text())
with TestClient(create_workbench_app(root/'runtime',generator=UnavailableGenerator(),auth_mode='test')) as client:
    bench=client.app.state.workbench; matter=bench.matter(expected['slug'],'development-taylor-morgan')
    store=bench.source_store(matter)
    for key,version,units in [('id','version','units'),('fresh_id','fresh_version','fresh_units')]:
        document=store.get(expected[key]); assert document.version_id==expected[version]
        assert [[u.number,u.location,u.text,u.excerpt_digest] for u in document.parsed_units()]==expected[units]
        assert client.get('/matters/'+matter.slug+'/sources/'+store.action_token(document)).status_code==200
    exported=client.get('/matters/'+matter.slug+'/conversations/'+expected['conversation']+'/export?format=markdown')
    assert exported.status_code==200 and 'Attachment contents are not fully searched' in exported.text
print('Stopped previous reader preserves both extraction versions and exports saved new coverage.')
'''
        checkpoint_reader = '''
import json,sys
from pathlib import Path
sys.path.insert(0,str(Path(sys.argv[1])/'src'))
from case_intelligence.workspace_store import WorkspaceStore
root=Path(sys.argv[2]); expected=json.loads((root/'expected.json').read_text())
store=WorkspaceStore(root/'runtime'/'workbench.sqlite')
job=store.research_job(expected['matter_id'],'development-taylor-morgan',expected['checkpoint_job'])
assert job.result['retrieval_source_fingerprint']==expected['source_fingerprint']
store.close()
print('Stopped previous reader preserves optional research boundary metadata.')
'''
        subprocess.run([sys.executable, "-c", checkpoint_reader, str(previous), str(root)], check=True)
        # The checkpoint proof above is read-only. Retire the synthetic active
        # job before either full application starts its normal recovery worker.
        from case_intelligence.workspace_store import WorkspaceStore
        workspace = WorkspaceStore(root / "runtime" / "workbench.sqlite")
        checkpoint_job = workspace.research_job(expected["matter_id"], ACTOR, expected["checkpoint_job"])
        assert checkpoint_job.result["retrieval_source_fingerprint"] == expected["source_fingerprint"]
        workspace.fail_research_job(checkpoint_job.job_id, "Generated compatibility drill finished.")
        workspace.close()
        subprocess.run([sys.executable, "-c", old_reader, str(previous), str(root)], check=True)
        with TestClient(create_workbench_app(root / "runtime", generator=UnavailableGenerator(), auth_mode="test")) as client:
            bench = client.app.state.workbench
            matter = bench.matter(expected["slug"], ACTOR)
            store = bench.source_store(matter)
            assert len(store.documents) == 2
            for identity, units in ((expected["id"], expected["units"]), (expected["fresh_id"], expected["fresh_units"])):
                document = store.get(identity)
                assert [[u.number, u.location, u.text, u.excerpt_digest] for u in document.parsed_units()] == units
                assert store.source_path(identity).read_bytes() == original
            assert bench.workspace.matter_readiness(matter.matter_id).email_count == 2
            resolved, units, index = bench._find_support(matter, expected["support"])
            assert resolved.version_id == expected["version"]
            assert units[index].text == expected["units"][-1][2]
            assert (root / "generated.eml").read_bytes() == original
        print("Forward read preserves exact old/new source units and original bytes without silent reprocessing.")


if __name__ == "__main__":
    main()
