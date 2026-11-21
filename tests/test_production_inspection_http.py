"""Inspect a browser selection through the real matter and upload boundaries."""
import hashlib

from fastapi.testclient import TestClient
import pytest

from case_intelligence.generation import UnavailableGenerator
from case_intelligence.intake_receipts import IntakeReceipts
from case_intelligence.managed_storage import StoragePolicy
from case_intelligence.workbench import create_workbench_app
from tests.test_case_lifecycle_web import login
from tests.test_load_file_admission import ACTOR, DAT, OPT, CountingScanner, app_at, new_matter


def inspect(client, matter, suffix, body, **headers):
    return client.post(f"/matters/{matter.slug}/production-inspection/{suffix}", content=body,
                       headers={"Content-Type": "application/octet-stream", **headers})


@pytest.mark.parametrize("suffix,body", [
    ("dat", DAT), ("opt", OPT), ("lfp", b"IM,P001,D,0,page.tif;2\n"),
    ("dii", b"@FULLTEXT DOC\n@T DOC1\n@BATESBEG P001\n@D @I\\IMAGES\\\npage.tif\n"),
], ids=["dat", "opt", "lfp", "dii"])
def test_inspection_scans_and_reads_without_admitting_a_source(tmp_path, suffix, body):
    scanner = CountingScanner()
    with TestClient(app_at(tmp_path / "runtime", scanner)) as client:
        bench, matter = new_matter(client)
        response = inspect(client, matter, suffix, body)
        assert response.status_code == 200, response.text
        assert response.headers["Cache-Control"] == "no-store"
        assert response.json()["format"] == suffix and response.json()["record_count"] == 1
        assert response.json()["sha256"] == hashlib.sha256(body).hexdigest()
        assert scanner.calls == [body]
        assert not IntakeReceipts(bench.workspace).recent(matter.matter_id, ACTOR)
        assert not bench.workspace.recent_upload_sessions(matter.matter_id, ACTOR)
        store = bench.source_store(matter)
        assert not store.documents and not list(store.staging.iterdir())


@pytest.mark.parametrize("outcome,status", [("infected", 422), ("unavailable", 503)])
def test_inspection_discards_bytes_after_scan_rejection(tmp_path, outcome, status):
    scanner = CountingScanner(outcome)
    with TestClient(app_at(tmp_path / "runtime", scanner)) as client:
        bench, matter = new_matter(client)
        response = inspect(client, matter, "dat", DAT)
        assert response.status_code == status and "rows" not in response.json()
        store = bench.source_store(matter)
        assert scanner.calls == [DAT] and not store.documents
        assert not list(store.staging.iterdir()) and not list(store.quarantine_root.iterdir())


def test_inspection_validates_whole_file_and_declared_and_actual_size(tmp_path):
    scanner = CountingScanner()
    policy = StoragePolicy(reserve_bytes=0, document_file_bytes=128, media_file_bytes=128,
                           upload_session_bytes=128, matter_quota_bytes=512)
    with TestClient(app_at(tmp_path / "runtime", scanner, policy=policy)) as client:
        bench, matter = new_matter(client)
        assert inspect(client, matter, "pdf", OPT).status_code == 415
        assert inspect(client, matter, "opt", OPT, **{"Content-Length": "129"}).status_code == 413
        assert inspect(client, matter, "opt", iter([OPT, b"x" * 128])).status_code == 413
        assert scanner.calls == []
        damaged = OPT + b"invalid tail\n"
        result = inspect(client, matter, "opt", damaged)
        assert result.status_code == 422 and "rows" not in result.json()
        assert scanner.calls == [damaged]
        assert not list(bench.source_store(matter).staging.iterdir())


def test_inspection_checks_required_scanner_readiness(tmp_path):
    scanner = CountingScanner(ready=False)
    with TestClient(app_at(tmp_path / "runtime", scanner)) as client:
        bench, matter = new_matter(client)
        response = inspect(client, matter, "dat", DAT)
        assert response.status_code == 503 and "rows" not in response.json()
        assert not scanner.calls and not list(bench.source_store(matter).staging.iterdir())


def test_inspection_requires_membership_and_real_csrf(tmp_path):
    app = create_workbench_app(tmp_path / "runtime", auth_mode="preview", generator=UnavailableGenerator(),
                               storage_policy=StoragePolicy(reserve_bytes=0), malware_scan_mode="disabled")
    with TestClient(app) as client:
        csrf = login(client)
        bench = client.app.state.workbench
        matter = bench.create_matter("Generated inspection", "", ACTOR)
        assert inspect(client, matter, "dat", DAT).status_code == 403
        assert inspect(client, matter, "dat", DAT, **{"X-CSRF-Token": csrf}).status_code == 200
        foreign = bench.create_matter("Separate inspection", "", "development-jordan-lee")
        assert inspect(client, foreign, "dat", DAT, **{"X-CSRF-Token": csrf}).status_code == 404
