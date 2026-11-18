from __future__ import annotations

import io
import json
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from case_intelligence.generation import UnavailableGenerator
from case_intelligence.ingestion import IngestionCoordinator
from case_intelligence.pilot_uploads import PdfOcrResult, PilotStore
from case_intelligence.review_bench_v2 import PdfPage
from case_intelligence.source_locations import (
    RegisteredSourceLocation,
    SourceLocationProblem,
    SourceLocationRegistry,
    SourceScanLimits,
)
from case_intelligence.workbench import CaseIntelligenceWorkbench, create_workbench_app
from case_intelligence.workspace_store import WorkspaceProblem, WorkspaceStore

WEB_ACTOR = "development-taylor-morgan"


def _matter(client: TestClient) -> str:
    response = client.post(
        "/matters",
        data={"name": "Collection review", "descriptor": "Synthetic acceptance matter"},
        follow_redirects=False,
    )
    return response.headers["location"].split("/")[2]


def test_registered_source_preflight_scales_hides_root_and_stages_stably(tmp_path):
    source_root = tmp_path / "registered"
    collection = source_root / "Synthetic Batch 01"
    collection.mkdir(parents=True)
    for ordinal in range(1_000):
        (collection / f"item-{ordinal:04d}.txt").write_text(
            f"synthetic discovery record {ordinal}\n", encoding="utf-8"
        )
    (collection / "unsupported.bin").write_bytes(b"not selected")
    registry = SourceLocationRegistry(
        (RegisteredSourceLocation("sample-source", "Synthetic source collection", source_root),),
        limits=SourceScanLimits(max_files=2_000),
    )

    preflight = registry.preflight("sample-source", "Synthetic Batch 01")
    assert len(preflight.supported) == 1_000
    assert preflight.unsupported_count == 1
    assert str(source_root) not in json.dumps(registry.staff_locations())

    staging = tmp_path / "staging"
    staging.mkdir()
    first = preflight.supported[0]
    original = (source_root / first.relative_path).read_bytes()
    staged = registry.stage("sample-source", first, staging)
    assert staged.path.read_bytes() == original
    assert (source_root / first.relative_path).read_bytes() == original
    staged.path.unlink()

    (source_root / first.relative_path).write_text("changed after review", encoding="utf-8")
    with pytest.raises(SourceLocationProblem, match="changed after review"):
        registry.stage("sample-source", first, staging)


def test_registered_source_preflight_rejects_symlinks_and_ambiguous_names(tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    root = tmp_path / "root"
    root.mkdir()
    (root / "linked.txt").symlink_to(outside)
    registry = SourceLocationRegistry(
        (RegisteredSourceLocation("safe-root", "Safe root", root),)
    )
    with pytest.raises(SourceLocationProblem, match="symbolic link"):
        registry.preflight("safe-root")

    (root / "linked.txt").unlink()
    (root / "Report.txt").write_text("one", encoding="utf-8")
    (root / "report.TXT").write_text("two", encoding="utf-8")
    with pytest.raises(SourceLocationProblem, match="ambiguous"):
        registry.preflight("safe-root")


def test_ingest_jobs_are_durable_claimed_and_recovered(tmp_path):
    workspace = WorkspaceStore(tmp_path / "workspace.sqlite")
    workspace.upsert_principal(
        "test",
        "development-user",
        "Development User",
        "development-user",
        preferred_principal_id="development-user",
    )
    matter = workspace.create_matter("Queue matter", "", "development-user")
    document_id = "a" * 32
    queued = workspace.queue_upload(matter.matter_id, document_id)
    assert queued.state == "queued"
    claimed = workspace.claim_ingest_job("worker-one")
    assert claimed is not None and claimed.state == "running" and claimed.attempts == 1
    assert workspace.recover_running_ingest_jobs() == 1
    reclaimed = workspace.claim_ingest_job("worker-two")
    assert reclaimed is not None and reclaimed.job_id == queued.job_id
    assert reclaimed.attempts == 2
    workspace.update_ingest_job(
        reclaimed.job_id, stage="Extracting text", completed_units=1, total_units=2
    )
    workspace.finish_ingest_job(reclaimed.job_id, succeeded=False, message="Try again")
    assert workspace.ingest_job(matter.matter_id, document_id).state == "failed"
    workspace.retry_ingest_job(matter.matter_id, document_id)
    assert workspace.ingest_job(matter.matter_id, document_id).state == "queued"
    workspace.close()


def test_ingest_claims_round_robin_across_large_and_small_matters(tmp_path):
    workspace = WorkspaceStore(tmp_path / "workspace.sqlite")
    workspace.upsert_principal(
        "test",
        "development-user",
        "Development User",
        "development-user",
        preferred_principal_id="development-user",
    )
    large = workspace.create_matter("Large production", "", "development-user")
    small = workspace.create_matter("Small urgent review", "", "development-user")
    for ordinal in range(3):
        workspace.queue_upload(large.matter_id, f"{ordinal + 1:032x}")
    for ordinal in range(2):
        workspace.queue_upload(small.matter_id, f"{ordinal + 101:032x}")

    claims = [workspace.claim_ingest_job(f"worker-{ordinal}") for ordinal in range(5)]
    assert all(claim is not None for claim in claims)
    assert [claim.matter_id for claim in claims] == [
        large.matter_id,
        small.matter_id,
        large.matter_id,
        small.matter_id,
        large.matter_id,
    ]
    workspace.close()


def test_media_claims_round_robin_across_matters(tmp_path):
    workspace = WorkspaceStore(tmp_path / "workspace.sqlite")
    workspace.upsert_principal(
        "test",
        "development-user",
        "Development User",
        "development-user",
        preferred_principal_id="development-user",
    )
    large = workspace.create_matter("Large media set", "", "development-user")
    small = workspace.create_matter("Small media set", "", "development-user")
    for matter, offsets in ((large, range(3)), (small, range(100, 102))):
        for ordinal in offsets:
            workspace.queue_media_job(
                matter.matter_id,
                f"{ordinal + 1:032x}",
                f"{ordinal + 1_000:032x}",
                "development-user",
                source_sha256=f"{ordinal + 2_000:064x}",
                byte_size=1,
                media_type="audio/mpeg",
                duration_ms=1_000,
            )

    claims = [workspace.claim_media_job(f"media-worker-{ordinal}") for ordinal in range(5)]
    assert all(claim is not None for claim in claims)
    assert [claim.matter_id for claim in claims] == [
        large.matter_id,
        small.matter_id,
        large.matter_id,
        small.matter_id,
        large.matter_id,
    ]
    workspace.close()


def test_scalable_ingestion_migration_matches_operator_copy():
    root = Path(__file__).parents[1]
    assert (
        root / "src/case_intelligence/migrations/sqlite/0003_scalable_ingestion.sql"
    ).read_bytes() == (root / "migrations/sqlite/0003_scalable_ingestion.sql").read_bytes()


def test_fair_ingest_scheduling_migration_matches_operator_copy():
    root = Path(__file__).parents[1]
    assert (
        root
        / "src/case_intelligence/migrations/sqlite/0019_fair_ingest_scheduling.sql"
    ).read_bytes() == (
        root / "migrations/sqlite/0019_fair_ingest_scheduling.sql"
    ).read_bytes()


def test_background_registered_folder_becomes_searchable_without_modifying_source(tmp_path):
    source_root = tmp_path / "registered"
    collection = source_root / "Matter export"
    collection.mkdir(parents=True)
    source = collection / "witness.txt"
    original = b"The synthetic witness identified the cobalt notebook at 8:14 p.m.\n"
    source.write_bytes(original)
    registry = SourceLocationRegistry(
        (RegisteredSourceLocation("review-share", "Review share", source_root),)
    )
    app = create_workbench_app(
        tmp_path / "runtime",
        generator=UnavailableGenerator(),
        source_registry=registry,
        background_ingestion=True,
        ingestion_workers=2,
        auth_mode="test",
    )
    with TestClient(app) as client:
        slug = _matter(client)
        preflight = client.post(
            f"/matters/{slug}/registered-sources/preflight",
            data={"source_location_id": "review-share", "relative_folder": "Matter export"},
            follow_redirects=False,
        )
        assert preflight.status_code == 303
        query = parse_qs(urlparse(preflight.headers["location"]).query)
        plan_id = query["plan"][0]
        review = client.get(preflight.headers["location"])
        assert "supported source" in review.text and "<strong>1</strong>" in review.text
        assert str(source_root) not in review.text

        confirmed = client.post(
            f"/matters/{slug}/registered-sources/{plan_id}/confirm",
            follow_redirects=False,
        )
        assert confirmed.status_code == 303
        matter = client.app.state.workbench.matter(slug, WEB_ACTOR)
        store = client.app.state.workbench.source_store(matter)
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            # Extraction precedes indexing and staging cleanup. The same
            # durable readiness contract as the query route must permit search.
            if client.app.state.workbench.workspace.matter_readiness(
                matter.matter_id
            ).state == "ready":
                break
            time.sleep(0.05)
        assert client.app.state.workbench.workspace.matter_readiness(
            matter.matter_id
        ).state == "ready"
        assert {item.state for item in store.documents.values()} == {"ready"}
        assert source.read_bytes() == original
        result = client.get(f"/matters/{slug}", params={"q": "cobalt notebook"})
        assert "witness.txt" in result.text and "8:14 p.m." in result.text
        assert str(source_root) not in result.text

        document = next(iter(store.documents.values()))
        removed = client.post(
            f"/matters/{slug}/sources/{store.action_token(document)}/remove",
            follow_redirects=False,
        )
        assert removed.status_code == 303
        assert source.read_bytes() == original


def test_registered_staging_cleanup_blocks_search_readiness_and_purge(
    tmp_path, monkeypatch
):
    source_root = tmp_path / "registered"
    collection = source_root / "Generated purge boundary"
    collection.mkdir(parents=True)
    (collection / "generated-record.txt").write_text(
        "Generated registered source for the cleanup boundary.\n",
        encoding="utf-8",
    )
    registry = SourceLocationRegistry(
        (RegisteredSourceLocation("review-share", "Review share", source_root),)
    )
    cleanup_started = threading.Event()
    release_cleanup = threading.Event()
    real_cleanup = IngestionCoordinator._remove_staged_source

    def paused_cleanup(coordinator, staged_path):
        cleanup_started.set()
        assert release_cleanup.wait(timeout=5)
        return real_cleanup(coordinator, staged_path)

    monkeypatch.setattr(
        IngestionCoordinator, "_remove_staged_source", paused_cleanup
    )
    app = create_workbench_app(
        tmp_path / "runtime",
        generator=UnavailableGenerator(),
        source_registry=registry,
        background_ingestion=True,
        ingestion_workers=1,
        auth_mode="test",
    )
    with TestClient(app) as client:
        slug = _matter(client)
        preflight = client.post(
            f"/matters/{slug}/registered-sources/preflight",
            data={
                "source_location_id": "review-share",
                "relative_folder": "Generated purge boundary",
            },
            follow_redirects=False,
        )
        plan_id = parse_qs(urlparse(preflight.headers["location"]).query)["plan"][0]
        assert client.post(
            f"/matters/{slug}/registered-sources/{plan_id}/confirm",
            follow_redirects=False,
        ).status_code == 303
        assert cleanup_started.wait(timeout=5)
        bench = client.app.state.workbench
        matter = bench.matter(slug, WEB_ACTOR)
        try:
            assert {item.state for item in bench.source_store(matter).documents.values()} == {
                "ready"
            }
            readiness = bench.workspace.matter_readiness(matter.matter_id)
            assert readiness.state == "preparing"
            assert readiness.processing_count == 1 and readiness.searchable_count == 0
            blocked = client.get(
                f"/matters/{slug}", params={"q": "Generated registered source"}
            )
            assert "Preparing your matter" in blocked.text
            assert bench.workspace.active_matter_work_counts(matter.matter_id)[
                "ingestion"
            ] == 1
            with pytest.raises(WorkspaceProblem, match="source and media processing"):
                bench.begin_matter_purge(
                    matter.slug, matter.owner_id, matter.display_name
                )
            assert bench.workspace.matter_lifecycle(matter.matter_id).state == "active"
        finally:
            release_cleanup.set()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if not bench.workspace.active_matter_work_counts(matter.matter_id)[
                "ingestion"
            ]:
                break
            time.sleep(0.01)
        assert not bench.workspace.active_matter_work_counts(matter.matter_id)[
            "ingestion"
        ]
        assert list(bench.storage.ingestion_staging.iterdir()) == []
        readiness = bench.workspace.matter_readiness(matter.matter_id)
        assert readiness.state == "ready" and readiness.searchable_count == 1
        assert readiness.processing_count == 0
        result = client.get(
            f"/matters/{slug}", params={"q": "Generated registered source"}
        )
        assert "generated-record.txt" in result.text
        assert "Generated registered source for the cleanup boundary." in result.text


def test_expanded_ocr_processes_more_than_legacy_25_page_cap(tmp_path, monkeypatch):
    attempted: list[int] = []
    monkeypatch.setenv("CASE_INTELLIGENCE_OCR_MODE", "expanded")
    monkeypatch.setenv("CASE_INTELLIGENCE_MAX_OCR_PAGES", "50")
    monkeypatch.setattr(
        "case_intelligence.review_bench_v2.extract_pdf_pages",
        lambda _path: tuple(PdfPage(number, "") for number in range(1, 31)),
    )

    def recognize(_path: Path, page_number: int) -> PdfOcrResult:
        attempted.append(page_number)
        return PdfOcrResult(f"Recognized synthetic page {page_number}", "recognized")

    monkeypatch.setattr("case_intelligence.pilot_uploads._ocr_pdf_page", recognize)
    store = PilotStore(tmp_path / "store")
    document, _ = store.store_stream(
        "scan.pdf", "application/pdf", io.BytesIO(b"%PDF-synthetic")
    )
    assert attempted == list(range(1, 31))
    assert document.state == "ready" and document.page_count == 30


def test_searchable_units_are_externalized_per_document(tmp_path):
    root = tmp_path / "store"
    store = PilotStore(root)
    document, _ = store.store_stream(
        "notes.txt", "text/plain", io.BytesIO(b"scalable derived text\n")
    )
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    assert manifest == {
        "version": 5,
        "storage": "source-registry.sqlite3",
        "schema_version": 1,
    }
    with sqlite3.connect(root / "source-registry.sqlite3") as connection:
        record = json.loads(
            connection.execute(
                "SELECT payload FROM source_document WHERE document_id=?",
                (document.document_id,),
            ).fetchone()[0]
        )
    assert record["units"] == []
    assert record["units_file"] == f"{document.document_id}.json"
    assert "scalable derived text" not in (root / "manifest.json").read_text(encoding="utf-8")
    assert "scalable derived text" not in json.dumps(record)
    restarted = PilotStore(root)
    assert restarted.get(document.document_id).parsed_units()[0].text == "scalable derived text"


def test_legacy_manifest_migrates_atomically_to_transactional_registry(tmp_path):
    root = tmp_path / "store"
    original = PilotStore(root)
    document, _ = original.store_stream(
        "legacy.txt", "text/plain", io.BytesIO(b"legacy migration text\n")
    )
    fields = (
        "document_id", "display_name", "stored_name", "media_type", "size", "state",
        "message", "units", "digest", "version_id", "name_key", "page_count",
        "units_file", "origin", "source_location_id", "relative_path", "stable_device",
        "stable_inode", "stable_mtime_ns", "processing_stage", "completed_units",
        "total_units", "duration_ms", "has_video", "playback_name",
        "playback_media_type", "playback_size", "playback_state", "playback_message",
    )
    legacy = {
        "version": 4,
        "documents": [{name: getattr(document, name) for name in fields}],
    }
    original.close()
    (root / "source-registry.sqlite3").unlink()
    (root / "manifest.json").write_text(json.dumps(legacy), encoding="utf-8")

    migrated = PilotStore(root)
    assert migrated.get(document.document_id).display_name == "legacy.txt"
    assert migrated.get(document.document_id).parsed_units()[0].text == "legacy migration text"
    assert json.loads((root / "manifest.json").read_text(encoding="utf-8"))["version"] == 5
    with sqlite3.connect(root / "source-registry.sqlite3") as connection:
        assert connection.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert connection.execute("SELECT COUNT(*) FROM source_document").fetchone()[0] == 1


def test_source_registry_emits_only_changed_documents_and_removals(tmp_path):
    changes = []
    store = PilotStore(tmp_path / "store", on_change=changes.append)
    first, _ = store.store_stream(
        "first.txt", "text/plain", io.BytesIO(b"first source\n")
    )
    second, _ = store.store_stream(
        "second.txt", "text/plain", io.BytesIO(b"second source\n")
    )
    assert tuple(item.document_id for item in changes[-1].upserted) == (second.document_id,)
    assert changes[-1].removed_document_ids == ()

    store.mark_failed(first.document_id, "Generated state transition")
    assert tuple(item.document_id for item in changes[-1].upserted) == (first.document_id,)
    assert changes[-1].removed_document_ids == ()

    store.remove(first.document_id)
    assert changes[-1].upserted == ()
    assert changes[-1].removed_document_ids == (first.document_id,)


def test_source_registry_marker_and_database_fail_closed(tmp_path):
    root = tmp_path / "store"
    store = PilotStore(root)
    store.close()

    marker = root / "manifest.json"
    registry = root / "source-registry.sqlite3"
    registry.unlink()
    with pytest.raises(RuntimeError, match="registry is unavailable"):
        PilotStore(root)

    replacement = PilotStore(tmp_path / "replacement")
    replacement.close()
    registry.symlink_to(tmp_path / "replacement" / "source-registry.sqlite3")
    with pytest.raises(RuntimeError, match="not a regular file"):
        PilotStore(root)
    registry.unlink()

    (tmp_path / "replacement" / "source-registry.sqlite3").replace(registry)
    marker.write_text(
        json.dumps(
            {"version": 5, "storage": "unexpected.sqlite3", "schema_version": 1}
        ),
        encoding="utf-8",
    )
    with pytest.raises(RuntimeError, match="manifest could not be loaded"):
        PilotStore(root)


def test_parallel_ingestion_serializes_the_shared_postgres_connection(monkeypatch):
    bench = object.__new__(CaseIntelligenceWorkbench)
    bench.postgres_ready = True
    bench.postgres_connection = object()
    bench._postgres_lock = threading.RLock()
    bench.embedding_index_warning = ""
    bench.embedding = object()
    bench.learned_retrieval = True
    matter = SimpleNamespace(matter_id="ci-matter-" + "a" * 32, display_name="Synthetic")
    documents = tuple(
        SimpleNamespace(
            document_id=f"{ordinal:032x}",
            display_name=f"source-{ordinal}.txt",
            media_type="text/plain",
            version_id=f"{ordinal + 10:032x}",
            digest=f"{ordinal + 20:064x}",
            size=12,
            stored_name=f"{ordinal + 30:032x}",
            origin="upload",
            source_location_id="",
            relative_path="",
            parsed_units=lambda: (),
        )
        for ordinal in range(4)
    )
    state_lock = threading.Lock()
    active = 0
    maximum_active = 0
    started = threading.Barrier(len(documents))

    def fake_upsert(_connection, **_kwargs):
        nonlocal active, maximum_active
        with state_lock:
            active += 1
            maximum_active = max(maximum_active, active)
        time.sleep(0.03)
        with state_lock:
            active -= 1

    monkeypatch.setattr("case_intelligence.workbench.upsert_postgres_document", fake_upsert)

    def index(document):
        started.wait(timeout=2)
        bench._index_document(matter, document)

    with ThreadPoolExecutor(max_workers=len(documents)) as executor:
        tuple(executor.map(index, documents))
    assert maximum_active == 1
