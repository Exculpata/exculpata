"""A browser inventory remains distinct from the selected load-file original."""
import asyncio
import hashlib
import json

from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient

from case_intelligence.generation import UnavailableGenerator
from case_intelligence.intake_receipts import IntakeReceipts, MAX_METADATA_BATCH_BYTES, MAX_SELECTION_ITEMS
from case_intelligence.managed_storage import StoragePolicy
from case_intelligence.workbench import create_workbench_app
from tests.test_case_lifecycle_web import login
from tests.test_load_file_admission import ACTOR, OPT, CountingScanner, app_at, new_matter
from tests.test_production_inspection_http import inspect


def envelope(files, index=0, **metadata):
    return json.dumps({"index": index, "files": files, **metadata}, ensure_ascii=False).encode("utf-8")


def streaming_inspection(client, matter, chunks, headers):
    async def send_chunks():
        for chunk in chunks:
            yield chunk

    async def post():
        async with AsyncClient(transport=ASGITransport(app=client.app), base_url="http://testserver") as http:
            return await http.post(f"/matters/{matter.slug}/production-inspection/opt", content=send_chunks(),
                                   headers={"Content-Type": "application/octet-stream", **headers})

    return asyncio.run(post())


def test_manifest_precedes_only_the_index_bytes_and_uses_separate_size_budgets(tmp_path):
    scanner = CountingScanner()
    policy = StoragePolicy(reserve_bytes=0, document_file_bytes=len(OPT), media_file_bytes=len(OPT),
                           upload_session_bytes=len(OPT), matter_quota_bytes=2 * len(OPT))
    with TestClient(app_at(tmp_path / "runtime", scanner, policy=policy)) as client:
        bench, matter = new_matter(client)
        prefix = envelope([["Café/INDEX.OPT", True], ["Café/images/one.tif", True]])
        assert len(prefix) > policy.document_file_bytes
        header = {"X-Selection-Manifest-Bytes": str(len(prefix))}
        chunks = (prefix[:6], prefix[6:-3], prefix[-3:] + OPT[:5], OPT[5:])
        for response in (inspect(client, matter, "opt", prefix + OPT, **header),
                         streaming_inspection(client, matter, chunks, header)):
            assert response.status_code == 200, response.text
            report = response.json()
            assert report["sha256"] == hashlib.sha256(OPT).hexdigest()
            assert report["reference_qc"]["total"] == report["reference_qc"]["counts"]["matched"] == 1
        assert scanner.calls == [OPT, OPT]
        assert not IntakeReceipts(bench.workspace).recent(matter.matter_id, ACTOR)
        store = bench.source_store(matter)
        assert not store.documents and not list(store.staging.iterdir())


def test_route_reports_reference_outside_the_display_sample_and_excludes_other_roots(tmp_path):
    scanner = CountingScanner()
    with TestClient(app_at(tmp_path / "runtime", scanner)) as client:
        _, matter = new_matter(client)
        prefix = envelope([["Volume/pages.opt", True], ["Volume/images/one.tif", True],
                           ["Separate/images/other.tif", True]])
        body = OPT * 20 + b"SYN-0021,VOL,images/other.tif,Y,,,1\n"
        response = inspect(client, matter, "opt", prefix + body,
                           **{"X-Selection-Manifest-Bytes": str(len(prefix))})
        assert response.status_code == 200, response.text
        report = response.json()
        assert report["record_count"] == 21 and len(report["rows"]) == 20
        qc = report["reference_qc"]
        assert qc["total"] == 21 and qc["counts"]["matched"] == 20 and qc["counts"]["missing"] == 1
        assert any(row["status"] == "missing" and row["ordinal"] == 21 for row in qc["examples"])
        assert scanner.calls == [body]


def test_inspected_opt_matches_canonically_equivalent_paths_and_preserves_the_supplied_path(tmp_path):
    reference = "Volume\u0301/Images/cafe\u0301.png"
    body = f"SYN0001,VOL,{reference},Y,,,1\n".encode()
    files = [["Volumé/Data/pages.opt", True], ["Volumé/Images/café.png", True],
             ["Other/Images/café.png", True]]
    with TestClient(app_at(tmp_path / "runtime", CountingScanner())) as client:
        _, matter = new_matter(client)
        for selected, expected in ((files, "matched"), (files + [["Volumé/Volumé/Images/café.png", True]], "ambiguous")):
            prefix = envelope(selected)
            response = inspect(client, matter, "opt", prefix + body,
                               **{"X-Selection-Manifest-Bytes": str(len(prefix))})
            assert response.status_code == 200, response.text
            report = response.json()
            assert report["reference_qc"]["counts"][expected] == report["reference_qc"]["total"] == 1
            assert report["rows"][0]["references"] == [{"role": "image", "path": reference, "status": expected}]


def test_invalid_manifest_never_reaches_the_scanner(tmp_path):
    scanner = CountingScanner()
    invalid_inputs = (
        (b"{}", "-1", 400), (b"{}", "0", 400),
        (b"{}", str(MAX_METADATA_BATCH_BYTES + 1), 413),
        (b"{}", "99", 400), (b"{}", "2", 400), (bytes([255]), "1", 400),
        (b'{"index":1,"index":0,"files":[["Volume/index.opt",true]]}', None, 400),
        (envelope([["Volume/index.dat", True]]), None, 400),
        (envelope([["Volume/index.opt", False]]), None, 400),
        (envelope([["Volume/index.opt", True]] * (MAX_SELECTION_ITEMS + 1)), None, 400),
    )
    with TestClient(app_at(tmp_path / "runtime", scanner)) as client:
        bench, matter = new_matter(client)
        for prefix, header, status in invalid_inputs:
            response = inspect(client, matter, "opt", prefix + OPT,
                               **{"X-Selection-Manifest-Bytes": header if header is not None else str(len(prefix))})
            assert response.status_code == status and set(response.json()) == {"message"}
        truncated = streaming_inspection(client, matter, (b"{",), {"X-Selection-Manifest-Bytes": "30"})
        assert truncated.status_code == 400
        assert not scanner.calls and not list(bench.source_store(matter).staging.iterdir())


def test_unavailable_scanner_and_excess_index_bytes_cannot_return_coverage(tmp_path):
    scanner = CountingScanner(ready=False)
    policy = StoragePolicy(reserve_bytes=0, document_file_bytes=len(OPT), media_file_bytes=len(OPT),
                           upload_session_bytes=len(OPT), matter_quota_bytes=2 * len(OPT))
    with TestClient(app_at(tmp_path / "runtime", scanner, policy=policy)) as client:
        bench, matter = new_matter(client)
        prefix = envelope([["Volume/index.opt", True]])
        header = {"X-Selection-Manifest-Bytes": str(len(prefix))}
        refused = inspect(client, matter, "opt", prefix + OPT, **header)
        assert refused.status_code == 503 and "reference_qc" not in refused.json()
        scanner.ready = True
        for wire in (prefix + OPT + b"x", iter((prefix, OPT, b"x"))):
            response = inspect(client, matter, "opt", wire, **header)
            assert response.status_code == 413 and "reference_qc" not in response.json()
        assert scanner.calls == [] and not list(bench.source_store(matter).staging.iterdir())


def test_mapped_reference_audit_requires_the_inspected_dat_and_its_actual_headings(tmp_path):
    scanner = CountingScanner()
    with TestClient(app_at(tmp_path / "runtime", scanner)) as client:
        bench, matter = new_matter(client)
        body = b"First label\x14Produced native\x14Delivered text\nPAGE07\x14native/a.pdf\x14text/a.txt\n"
        files = [["VOL/data/index.dat", True], ["VOL/native/a.pdf", True], ["VOL/text/a.txt", False]]
        digest = hashlib.sha256(body).hexdigest()
        roles = {"First label": "begin_bates", "Produced native": "native_path", "Delivered text": "text_path"}

        def audit(suffix, selected, original, sha256, fields):
            prefix = envelope(selected, dat_profile={"sha256": sha256, "fields": fields})
            return inspect(client, matter, suffix, prefix + original,
                           **{"X-Selection-Manifest-Bytes": str(len(prefix))})

        good = audit("dat", files, body, digest, roles)
        assert good.status_code == 200, good.text
        report = good.json()
        assert report["sha256"] == digest and report["reference_qc"]["total"] == 2
        assert report["reference_qc"]["counts"]["matched"] == report["reference_qc"]["counts"]["ineligible"] == 1
        changed = audit("dat", files, body.replace(b"PAGE07", b"PAGE08"), digest, roles)
        absent = audit("dat", files, body, digest, {"Earlier heading": "native_path"})
        wrong_format = audit("opt", [["VOL/index.opt", True]], OPT, hashlib.sha256(OPT).hexdigest(), roles)
        assert changed.status_code == absent.status_code == 409
        assert wrong_format.status_code == 400
        assert all(set(response.json()) == {"message"} for response in (changed, absent, wrong_format))
        assert not list(bench.source_store(matter).staging.iterdir())


def test_streamed_manifest_rejects_a_malformed_or_inconsistent_content_length(tmp_path):
    scanner = CountingScanner()
    with TestClient(app_at(tmp_path / "runtime", scanner)) as client:
        bench, matter = new_matter(client)
        prefix = envelope([["Volume/index.opt", True]])
        size = len(prefix) + len(OPT)
        for declared in ("invalid", "-1", str(len(prefix) - 1), str(size - 1), str(size + 1)):
            response = streaming_inspection(client, matter, (prefix, OPT[:2], OPT[2:]), {
                "X-Selection-Manifest-Bytes": str(len(prefix)), "Content-Length": declared})
            assert response.status_code == 400 and set(response.json()) == {"message"}
        assert not scanner.calls
        assert not list(bench.source_store(matter).staging.iterdir())


def test_access_revoked_during_inspection_withholds_the_manifest_report(tmp_path, monkeypatch):
    scanner = CountingScanner()
    app = create_workbench_app(tmp_path / "runtime", auth_mode="preview", generator=UnavailableGenerator(),
                               malware_scanner=scanner, storage_policy=StoragePolicy(reserve_bytes=0))
    with TestClient(app) as client:
        owner_csrf = login(client)
        client.headers["X-CSRF-Token"] = owner_csrf
        bench, matter = new_matter(client)
        member_csrf = login(client, subject="jordan-lee")
        client.headers["X-CSRF-Token"] = member_csrf
        member = "development-jordan-lee"
        bench.workspace.add_member(matter.matter_id, member, ACTOR)
        normal_scan = scanner.scan

        def revoke_during_scan(path):
            bench.workspace.revoke_member(matter.matter_id, member, ACTOR)
            return normal_scan(path)

        monkeypatch.setattr(scanner, "scan", revoke_during_scan)
        prefix = envelope([["Volume/index.opt", True], ["Volume/images/one.tif", True]])
        response = inspect(client, matter, "opt", prefix + OPT, **{"X-Selection-Manifest-Bytes": str(len(prefix))})
        assert response.status_code == 404 and "reference_qc" not in response.text
        assert scanner.calls == [OPT] and not list(bench.source_store(matter).staging.iterdir())
