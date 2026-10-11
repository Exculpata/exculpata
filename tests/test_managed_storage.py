from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from case_intelligence.generation import UnavailableGenerator
from case_intelligence.managed_storage import (
    MARKER_NAME,
    ManagedMatterStorage,
    StoragePolicy,
)
from case_intelligence.pilot_uploads import PilotStore
from case_intelligence.workbench import create_workbench_app


def _matter(client: TestClient) -> str:
    response = client.post(
        "/matters",
        data={"name": "Managed storage matter", "descriptor": "Synthetic capacity test"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    return response.headers["location"].split("/")[2]


def test_external_storage_requires_valid_marker_and_owned_directories(tmp_path):
    root = tmp_path / "managed"
    root.mkdir()
    policy = StoragePolicy(100, 80, 80, 40, 0)
    with pytest.raises(RuntimeError, match="marker"):
        ManagedMatterStorage(root, policy=policy, require_marker=True)

    root.rmdir()
    storage = ManagedMatterStorage.initialize(root, policy=policy)
    assert storage.matters.is_dir()
    assert storage.purging.is_dir()
    assert storage.ingestion_staging.is_dir()
    assert storage.capacity().ready is True

    marker = root / MARKER_NAME
    payload = json.loads(marker.read_text(encoding="utf-8"))
    payload["product"] = "Not RecordBench"
    marker.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(RuntimeError, match="marker"):
        ManagedMatterStorage(root, policy=policy, require_marker=True)


def test_usage_projection_tolerates_an_atomic_file_rename(tmp_path, monkeypatch):
    root = tmp_path / "usage"
    root.mkdir()

    class VanishedEntry:
        path = str(root / ".manifest-generated.tmp")

        @staticmethod
        def is_symlink() -> bool:
            return False

        @staticmethod
        def stat(*, follow_symlinks: bool):
            assert follow_symlinks is False
            raise FileNotFoundError(VanishedEntry.path)

    class GeneratedScan:
        def __enter__(self):
            return iter((VanishedEntry(),))

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(
        "case_intelligence.managed_storage.os.scandir", lambda _directory: GeneratedScan()
    )
    assert ManagedMatterStorage._tree_usage(root) == 0


def test_storage_policy_environment_is_strict_and_relational(monkeypatch):
    monkeypatch.setenv("CASE_INTELLIGENCE_MATTER_QUOTA_GIB", "250")
    monkeypatch.setenv("CASE_INTELLIGENCE_UPLOAD_COLLECTION_GIB", "100")
    monkeypatch.setenv("CASE_INTELLIGENCE_MEDIA_FILE_GIB", "80")
    monkeypatch.setenv("CASE_INTELLIGENCE_DOCUMENT_FILE_MIB", "768")
    monkeypatch.setenv("CASE_INTELLIGENCE_STORAGE_RESERVE_GIB", "50")
    policy = StoragePolicy.from_environment()
    assert policy.matter_quota_bytes == 250 * 1024**3
    assert policy.document_file_bytes == 768 * 1024**2
    assert policy.reserve_bytes == 50 * 1024**3

    monkeypatch.setenv("CASE_INTELLIGENCE_UPLOAD_COLLECTION_GIB", "251")
    with pytest.raises(ValueError, match="matter quota"):
        StoragePolicy.from_environment()
    monkeypatch.setenv("CASE_INTELLIGENCE_UPLOAD_COLLECTION_GIB", "not-a-number")
    with pytest.raises(RuntimeError, match="whole positive"):
        StoragePolicy.from_environment()


def test_resumable_finalize_retains_retry_link_without_duplicate_source_bytes(tmp_path):
    store = PilotStore(
        tmp_path / "store",
        document_file_limit=100,
        media_file_limit=100,
        upload_session_limit=100,
    )
    item_id = "upload-item-" + "a" * 32
    body = b"copy-free synthetic source\n"
    store.append_resumable_chunk(
        item_id,
        offset=0,
        expected_size=len(body),
        chunk=body,
    )
    incoming = store.incoming / (".session-" + "a" * 32 + ".part")
    document = store.finalize_resumable_upload(
        item_id,
        display_name="notes.txt",
        relative_path="notes.txt",
        content_type="text/plain",
        expected_size=len(body),
    )
    final = store.source_path(document.document_id)
    assert incoming.read_bytes() == body
    assert final.read_bytes() == body
    assert os.stat(incoming).st_ino == os.stat(final).st_ino
    store.discard_resumable_upload(item_id)
    assert not incoming.exists()
    assert final.read_bytes() == body


def test_managed_root_quota_reservations_and_staff_capacity_ui(tmp_path):
    runtime = tmp_path / "runtime"
    managed = tmp_path / "managed"
    policy = StoragePolicy(
        matter_quota_bytes=50,
        upload_session_bytes=40,
        media_file_bytes=40,
        document_file_bytes=40,
        reserve_bytes=0,
    )
    ManagedMatterStorage.initialize(managed, policy=policy)
    app = create_workbench_app(
        runtime,
        managed_storage_root=managed,
        storage_policy=policy,
        generator=UnavailableGenerator(),
        auth_mode="test",
        background_ingestion=True,
        ingestion_workers=1,
    )
    with TestClient(app) as client:
        slug = _matter(client)
        setup = client.get(f"/matters/{slug}/setup")
        assert setup.status_code == 200
        assert "50 B matter allowance" in setup.text
        assert 'data-max-collection-bytes="40"' in setup.text
        assert str(managed) not in setup.text

        first = client.post(
            f"/matters/{slug}/upload-sessions",
            json={
                "collection_name": "First reservation",
                "files": [
                    {
                        "name": "first.txt",
                        "relative_path": "first.txt",
                        "size": 30,
                        "media_type": "text/plain",
                    }
                ],
            },
        )
        assert first.status_code == 201
        second = client.post(
            f"/matters/{slug}/upload-sessions",
            json={
                "collection_name": "Would exceed matter",
                "files": [
                    {
                        "name": "second.txt",
                        "relative_path": "second.txt",
                        "size": 25,
                        "media_type": "text/plain",
                    }
                ],
            },
        )
        assert second.status_code == 413
        assert "20 B of upload capacity remaining" in second.json()["message"]

        bench = client.app.state.workbench
        matter = bench.matter(slug, "development-taylor-morgan")
        assert bench.workspace.pending_upload_bytes(matter.matter_id) == 30
        assert (managed / "matters" / matter.matter_id / "sources").is_dir()
        assert not (runtime / "matters" / matter.matter_id).exists()
        health = client.get("/health").json()
        assert health["storage"]["status"] == "ready"
        assert str(managed) not in json.dumps(health)


def test_managed_storage_activation_refuses_unmigrated_legacy_matter_bytes(tmp_path):
    runtime = tmp_path / "runtime"
    legacy = runtime / "matters" / ("ci-matter-" + "b" * 32)
    legacy.mkdir(parents=True)
    (legacy / "unexpected.txt").write_text("synthetic", encoding="utf-8")
    managed = tmp_path / "managed"
    policy = StoragePolicy(100, 80, 80, 40, 0)
    ManagedMatterStorage.initialize(managed, policy=policy)
    with pytest.raises(RuntimeError, match="explicit migration"):
        create_workbench_app(
            runtime,
            managed_storage_root=managed,
            storage_policy=policy,
            generator=UnavailableGenerator(),
            auth_mode="test",
        )


@pytest.mark.parametrize("names", ["recordbench", "exculpata"])
def test_both_marker_generations_open_without_rewriting(tmp_path, monkeypatch, names):
    monkeypatch.setenv("EXCULPATA_STORAGE_NAMES", names)
    root = tmp_path / "managed"
    policy = StoragePolicy(100, 80, 80, 40, 0)
    ManagedMatterStorage.initialize(root, policy=policy)
    marker = root / f".{names}-managed-storage.json"
    original = marker.read_bytes()
    payload = json.loads(original)
    assert payload["product"] == ("RecordBench" if names == "recordbench" else "Exculpata")
    assert payload["storage_id"].startswith(f"{names}-storage-")
    monkeypatch.setenv("EXCULPATA_STORAGE_NAMES", "exculpata" if names == "recordbench" else "recordbench")
    assert ManagedMatterStorage(root, policy=policy, require_marker=True).capacity().ready
    assert marker.read_bytes() == original
    with pytest.raises(RuntimeError, match="already initialized"):
        ManagedMatterStorage.initialize(root, policy=policy)
    assert marker.read_bytes() == original


@pytest.mark.parametrize("damage", ["symlink", "oversized", "wrong-product", "wrong-prefix", "two-markers"])
@pytest.mark.parametrize("names", ["recordbench", "exculpata"])
def test_marker_generation_does_not_weaken_validation(tmp_path, monkeypatch, names, damage):
    monkeypatch.setenv("EXCULPATA_STORAGE_NAMES", names)
    root = tmp_path / "managed"
    policy = StoragePolicy(100, 80, 80, 40, 0)
    ManagedMatterStorage.initialize(root, policy=policy)
    marker = root / f".{names}-managed-storage.json"
    payload = json.loads(marker.read_text())
    other = "recordbench" if names == "exculpata" else "exculpata"
    if damage == "symlink":
        copied = tmp_path / "synthetic-marker"
        marker.rename(copied)
        marker.symlink_to(copied)
    elif damage == "oversized":
        marker.write_text(" " * 4097 + json.dumps(payload))
    elif damage == "wrong-product":
        payload["product"] = "Synthetic invalid product"
        marker.write_text(json.dumps(payload))
    elif damage == "wrong-prefix":
        payload["storage_id"] = other + "-storage-synthetic"
        marker.write_text(json.dumps(payload))
    else:
        (root / f".{other}-managed-storage.json").write_bytes(marker.read_bytes())
    with pytest.raises(RuntimeError, match="marker"):
        ManagedMatterStorage(root, policy=policy, require_marker=True)
