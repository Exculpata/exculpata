"""A browser inventory remains distinct from the selected load-file original."""
import asyncio
import hashlib
import json

from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient

from case_intelligence.intake_receipts import IntakeReceipts, MAX_METADATA_BATCH_BYTES, MAX_SELECTION_ITEMS
from case_intelligence.managed_storage import StoragePolicy
from tests.test_load_file_admission import ACTOR, OPT, CountingScanner, app_at, new_matter
from tests.test_production_inspection_http import inspect


def envelope(files, index=0):
    return json.dumps({"index": index, "files": files}, ensure_ascii=False).encode("utf-8")


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
