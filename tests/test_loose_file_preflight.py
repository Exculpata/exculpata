from __future__ import annotations


from case_intelligence.legacy_names import (
    LEGACY_STEM,
)
import asyncio
import re
import threading

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from case_intelligence.generation import UnavailableGenerator
from case_intelligence.malware_scan import MalwareScanResult, MalwareScannerStatus
from case_intelligence.managed_storage import StoragePolicy
from case_intelligence.workbench import create_workbench_app


ACTOR = "development-taylor-morgan"
PREFLIGHT_REQUEST_BYTES = 6 * 1024 * 1024


class UnavailableCountingScanner:
    def __init__(self) -> None:
        self.status_calls = 0
        self.scan_calls = 0

    def status(self, *, force: bool = False) -> MalwareScannerStatus:
        del force
        self.status_calls += 1
        return MalwareScannerStatus(
            "unavailable",
            "synthetic",
            message="Synthetic scanner is unavailable.",
        )

    def scan(self, _path) -> MalwareScanResult:
        self.scan_calls += 1
        return MalwareScanResult("unavailable", "synthetic")


class ReadyCountingScanner(UnavailableCountingScanner):
    def status(self, *, force: bool = False) -> MalwareScannerStatus:
        del force
        self.status_calls += 1
        return MalwareScannerStatus("ready", "synthetic")


class EventLoopLivenessScanner(ReadyCountingScanner):
    def __init__(self) -> None:
        super().__init__()
        self.loop: asyncio.AbstractEventLoop | None = None
        self.released_by_loop = False
        self.timed_out = False

    def status(self, *, force: bool = False) -> MalwareScannerStatus:
        del force
        self.status_calls += 1
        assert self.loop is not None
        released = threading.Event()

        def release() -> None:
            self.released_by_loop = True
            released.set()

        self.loop.call_soon_threadsafe(release)
        if not released.wait(1):
            self.timed_out = True
        return MalwareScannerStatus("ready", "synthetic")


class RaisingStatusScanner(UnavailableCountingScanner):
    def status(self, *, force: bool = False) -> MalwareScannerStatus:
        del force
        self.status_calls += 1
        raise OSError("synthetic scanner status failure")


class EventLoopLivenessCapacityProbe:
    def __init__(self) -> None:
        self.loop: asyncio.AbstractEventLoop | None = None
        self.released_by_loop = False
        self.timed_out = False

    def __call__(self, _matter_id: str) -> int:
        assert self.loop is not None
        released = threading.Event()

        def release() -> None:
            self.released_by_loop = True
            released.set()

        self.loop.call_soon_threadsafe(release)
        if not released.wait(1):
            self.timed_out = True
        return 0


def _matter(client: TestClient, name: str = "Synthetic preflight matter") -> str:
    response = client.post(
        "/matters",
        data={"name": name, "descriptor": "Generated loose-file preflight acceptance"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    return response.headers["location"].split("/")[2]


def _app(tmp_path, scanner: UnavailableCountingScanner):
    return create_workbench_app(
        tmp_path / "runtime",
        generator=UnavailableGenerator(),
        auth_mode="test",
        background_ingestion=False,
        storage_policy=StoragePolicy(
            matter_quota_bytes=1_024,
            upload_session_bytes=512,
            media_file_bytes=256,
            document_file_bytes=128,
            reserve_bytes=0,
        ),
        malware_scanner=scanner,
        malware_scan_mode="extended",
    )


def _raw_preflight_request(
    app, slug: str, chunks, *, content_length: str | None = None
):
    messages = list(chunks)
    consumed_chunks = 0
    sent = []

    async def receive():
        nonlocal consumed_chunks
        consumed_chunks += 1
        return messages.pop(0)

    async def send(message):
        sent.append(message)

    path = f"/matters/{slug}/upload-preflight"
    headers = [
        (b"host", (f"{LEGACY_STEM}.example.test").encode()),
        (b"accept", b"application/json"),
        (b"content-type", b"application/json"),
        (b"x-csrf-token", b"test-csrf"),
    ]
    if content_length is not None:
        headers.append((b"content-length", content_length.encode("ascii")))
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.4"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "https",
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": b"",
        "root_path": "",
        "headers": headers,
        "client": ("127.0.0.1", 43110),
        "server": (f"{LEGACY_STEM}.example.test", 443),
        "state": {
            "auth": app.state.identity.resolve(None),
            "request_id": "synthetic-preflight-stream-limit",
        },
        "app": app,
    }
    try:
        # Exercise the middleware stack as well as routing; FastAPI owns its
        # request cleanup context there. Do not fabricate framework-private scope.
        asyncio.run(app(scope, receive, send))
    except HTTPException as exc:
        return exc.status_code, consumed_chunks
    response_start = next(
        message for message in sent if message["type"] == "http.response.start"
    )
    return response_start["status"], consumed_chunks


@pytest.mark.parametrize("content_length", [None, "1"], ids=["missing", "lying"])
def test_selection_preflight_stream_limit_stops_on_first_crossing_chunk(
    tmp_path, content_length
):
    app = _app(tmp_path, UnavailableCountingScanner())
    with TestClient(app) as client:
        slug = _matter(client)
        chunks = [
            {
                "type": "http.request",
                "body": b"x" * PREFLIGHT_REQUEST_BYTES,
                "more_body": True,
            },
            {"type": "http.request", "body": b"y", "more_body": True},
            {"type": "http.request", "body": b"not-consumed", "more_body": False},
        ]

        status, consumed_chunks = _raw_preflight_request(
            app, slug, chunks, content_length=content_length
        )

    assert status == 413
    assert consumed_chunks == 2


def test_selection_preflight_rejects_foreign_matter_before_consuming_body(tmp_path):
    app = _app(tmp_path, UnavailableCountingScanner())
    with TestClient(app) as client:
        foreign = client.app.state.workbench.create_matter(
            "Foreign synthetic matter",
            "Isolation stream canary",
            "development-jordan-lee",
        )
        chunks = [
            {"type": "http.request", "body": b"synthetic body", "more_body": False}
        ]

        status, consumed_chunks = _raw_preflight_request(app, foreign.slug, chunks)

    assert status == 404
    assert consumed_chunks == 0


def test_selection_preflight_scanner_status_does_not_block_the_event_loop(tmp_path):
    scanner = EventLoopLivenessScanner()
    app = _app(tmp_path, scanner)

    @app.middleware("http")
    async def capture_event_loop(request, call_next):
        scanner.loop = asyncio.get_running_loop()
        return await call_next(request)

    with TestClient(app) as client:
        slug = _matter(client)
        response = client.post(
            f"/matters/{slug}/upload-preflight",
            json={"files": [{"name": "notes.txt", "size": 12}]},
        )

    assert response.status_code == 200
    assert scanner.released_by_loop is True
    assert scanner.timed_out is False
    assert response.json()["eligible_indexes"] == [0]


def test_selection_preflight_capacity_projection_does_not_block_event_loop(
    tmp_path, monkeypatch
):
    scanner = ReadyCountingScanner()
    capacity_probe = EventLoopLivenessCapacityProbe()
    app = _app(tmp_path, scanner)

    @app.middleware("http")
    async def capture_event_loop(request, call_next):
        capacity_probe.loop = asyncio.get_running_loop()
        return await call_next(request)

    with TestClient(app) as client:
        slug = _matter(client)
        monkeypatch.setattr(
            client.app.state.workbench.storage,
            "matter_payload_usage_bytes",
            capacity_probe,
        )
        response = client.post(
            f"/matters/{slug}/upload-preflight",
            json={"files": [{"name": "notes.txt", "size": 12}]},
        )

    assert response.status_code == 200
    assert capacity_probe.released_by_loop is True
    assert capacity_probe.timed_out is False
    assert response.json()["matter_capacity"]["used_bytes"] == 0


def test_selection_preflight_preserves_unavailable_scanner_status_projection(tmp_path):
    scanner = RaisingStatusScanner()
    app = _app(tmp_path, scanner)
    with TestClient(app) as client:
        slug = _matter(client)
        response = client.post(
            f"/matters/{slug}/upload-preflight",
            json={"files": [{"name": "scan.png", "size": 68}]},
        )

    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["state"] == "needs_attention"
    assert item["scan"] == {
        "required": True,
        "capability": "unavailable",
        "result": "not_run",
    }
    assert scanner.status_calls == 1
    assert scanner.scan_calls == 0


def test_selection_preflight_duplicate_tokens_are_canonical_and_matter_bound(tmp_path):
    scanner = ReadyCountingScanner()
    app = _app(tmp_path, scanner)
    nonce = "0123456789abcdef0123456789abcdef"
    with TestClient(app) as client:
        first_slug = _matter(client, "First synthetic token matter")
        second_slug = _matter(client, "Second synthetic token matter")

        def preview(slug: str, path: str):
            return client.post(
                f"/matters/{slug}/upload-preflight",
                json={
                    "selection_nonce": nonce,
                    "files": [{"name": "record.txt", "relative_path": path, "size": 12}],
                },
            )

        first = preview(first_slug, "Production/Stra\u00dfe.txt")
        canonical_peer = preview(first_slug, "production/STRASSE.txt")
        other_matter = preview(second_slug, "Production/Stra\u00dfe.txt")
        unsafe = preview(first_slug, "../unsafe.txt")
        unsupported = preview(first_slug, "archive.zip")
        invalid_size = client.post(
            f"/matters/{first_slug}/upload-preflight",
            json={
                "selection_nonce": nonce,
                "files": [{"name": "record.txt", "size": 0}],
            },
        )

        before_invalid_nonce_calls = scanner.status_calls
        invalid_nonce = client.post(
            f"/matters/{first_slug}/upload-preflight",
            json={
                "selection_nonce": "NOT-A-VALID-NONCE",
                "files": [{"name": "record.txt", "size": 12}],
            },
        )

    assert first.status_code == canonical_peer.status_code == other_matter.status_code == 200
    first_token = first.json()["items"][0]["duplicate_token"]
    assert first_token == canonical_peer.json()["items"][0]["duplicate_token"]
    assert first_token != other_matter.json()["items"][0]["duplicate_token"]
    assert len(first_token) == 64
    assert all(character in "0123456789abcdef" for character in first_token)
    assert "duplicate_token" not in unsafe.json()["items"][0]
    assert "duplicate_token" not in unsupported.json()["items"][0]
    assert "duplicate_token" not in invalid_size.json()["items"][0]
    assert invalid_nonce.status_code == 400
    assert scanner.status_calls == before_invalid_nonce_calls


def test_selection_preflight_accounts_for_every_file_without_durable_writes(tmp_path):
    scanner = UnavailableCountingScanner()
    app = _app(tmp_path, scanner)
    with TestClient(app) as client:
        slug = _matter(client)
        bench = client.app.state.workbench
        matter = bench.matter(slug, ACTOR)
        before_collections = bench.workspace.source_collections(matter.matter_id)
        before_sessions = bench.workspace.recent_upload_sessions(matter.matter_id, ACTOR)
        before_reserved = bench.workspace.pending_upload_bytes(matter.matter_id)
        before_status_calls = scanner.status_calls

        response = client.post(
            f"/matters/{slug}/upload-preflight",
            json={
                "files": [
                    {
                        "name": "incident-notes.txt",
                        "relative_path": "ready/incident-notes.txt",
                        "size": 23,
                        "media_type": "text/plain",
                    },
                    {
                        "name": "damaged.pdf",
                        "relative_path": "ready/damaged.pdf",
                        "size": 19,
                        "media_type": "application/pdf",
                    },
                    {
                        "name": "scan.png",
                        "relative_path": "attention/scan.png",
                        "size": 68,
                        "media_type": "image/png",
                    },
                    {
                        "name": "messages.zip",
                        "relative_path": "unsupported/messages.zip",
                        "size": 4,
                        "media_type": "application/zip",
                    },
                    {
                        "name": "oversize.txt",
                        "relative_path": "over-limit/oversize.txt",
                        "size": 129,
                        "media_type": "text/plain",
                    },
                ]
            },
        )

        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        payload = response.json()
        assert payload["status"] == "partial"
        assert payload["selected_count"] == 5
        assert payload["counts"] == {
            "valid": 2,
            "needs_attention": 1,
            "unsupported": 1,
            "duplicate_candidate": 0,
            "over_limit": 1,
            "failed": 0,
        }
        assert [item["state"] for item in payload["items"]] == [
            "valid",
            "valid",
            "needs_attention",
            "unsupported",
            "over_limit",
        ]
        assert payload["eligible_indexes"] == [0, 1]
        assert payload["items"][0] == {
            "index": 0,
            "display_name": "incident-notes.txt",
            "path_safety_validated": True,
            "state": "valid",
            "eligible": True,
            "supplied_type": "text/plain",
            "expected_type": "text/plain",
            "detected_type": None,
            "size": 23,
            "readability": "pending_upload",
            "source_version": "pending_upload",
            "scan": {
                "required": False,
                "capability": "not_required",
                "result": "not_required",
            },
            "duplicate": "not_evaluated",
            "message": "Ready to upload. File contents will be checked after transfer.",
        }
        assert payload["items"][2]["scan"] == {
            "required": True,
            "capability": "unavailable",
            "result": "not_run",
        }
        assert "not run" in payload["items"][2]["message"].lower()
        assert payload["items"][3]["expected_type"] is None
        assert payload["items"][3]["detected_type"] is None
        assert payload["items"][3]["size"] == 4
        assert scanner.status_calls == before_status_calls + 1
        assert scanner.scan_calls == 0
        assert bench.workspace.source_collections(matter.matter_id) == before_collections
        assert bench.workspace.recent_upload_sessions(matter.matter_id, ACTOR) == before_sessions
        assert bench.workspace.pending_upload_bytes(matter.matter_id) == before_reserved
        preflight_event = next(
            event
            for event in bench.workspace.audit_events(matter.matter_id)
            if event.action == "source.upload_preflight"
        )
        assert preflight_event.details == {"count": 5, "result_count": 2}


def test_selection_preflight_reports_scoped_matter_capacity_without_writes(
    tmp_path, monkeypatch
):
    scanner = ReadyCountingScanner()
    app = _app(tmp_path, scanner)
    with TestClient(app) as client:
        slug = _matter(client, "Capacity snapshot matter")
        other_slug = _matter(client, "Other capacity snapshot matter")
        bench = client.app.state.workbench
        matter = bench.matter(slug, ACTOR)
        other = bench.matter(other_slug, ACTOR)

        def reserve(target, name: str, size: int, *, actor: str = ACTOR):
            session, _ = bench.create_upload_session(
                target,
                actor,
                f"Synthetic {name} reservation",
                (
                    {
                        "display_name": f"{name}.txt",
                        "relative_path": f"capacity/{name}.txt",
                        "media_type": "text/plain",
                        "expected_size": size,
                    },
                ),
            )
            return session

        checkpoint = reserve(matter, "checkpoint", 200)
        reserve(matter, "other", 300)
        foreign = reserve(other, "foreign", 400)
        other_actor = "development-jordan-lee"
        bench.workspace.add_member(matter.matter_id, other_actor, ACTOR)
        foreign_actor = reserve(matter, "foreign-actor", 50, actor=other_actor)
        cancelled = reserve(matter, "cancelled", 100)
        bench.workspace.cancel_upload_session(
            matter.matter_id, ACTOR, cancelled.upload_session_id
        )
        partial = reserve(matter, "partial", 75)
        _, partial_items = bench.workspace.upload_session(
            matter.matter_id, ACTOR, partial.upload_session_id
        )
        bench.workspace.fail_upload_item(
            matter.matter_id,
            ACTOR,
            partial.upload_session_id,
            partial_items[0].upload_item_id,
            "Synthetic terminal checkpoint",
        )
        monkeypatch.setattr(
            bench.storage,
            "matter_payload_usage_bytes",
            lambda matter_id: 111 if matter_id == matter.matter_id else 222,
        )

        def snapshot(**extra):
            before = (
                bench.workspace.source_collections(matter.matter_id),
                bench.workspace.recent_upload_sessions(matter.matter_id, ACTOR),
                bench.workspace.pending_upload_bytes(matter.matter_id),
            )
            response = client.post(
                f"/matters/{slug}/upload-preflight",
                json={
                    "selection_nonce": "c" * 32,
                    "files": [
                        {
                            "name": "small.txt",
                            "relative_path": "capacity/small.txt",
                            "size": 12,
                            "media_type": "text/plain",
                        }
                    ],
                    **extra,
                },
            )
            after = (
                bench.workspace.source_collections(matter.matter_id),
                bench.workspace.recent_upload_sessions(matter.matter_id, ACTOR),
                bench.workspace.pending_upload_bytes(matter.matter_id),
            )
            assert after == before
            return response

        ordinary = snapshot()
        assert ordinary.status_code == 200
        assert ordinary.json()["matter_capacity"] == {
            "version": 2,
            "quota_bytes": 1_024,
            "used_bytes": 111,
            "total_reserved_bytes": 550,
            "fresh_available_bytes": 363,
            "checkpoint_validated": False,
            "checkpoint_remaining_bytes": 0,
            "other_reserved_bytes": 550,
            "available_bytes": 363,
        }

        credited = snapshot(
            checkpoint_session_id=checkpoint.upload_session_id,
            checkpoint_collection_id=checkpoint.collection_id,
        )
        assert credited.status_code == 200
        assert credited.json()["matter_capacity"] == {
            "version": 2,
            "quota_bytes": 1_024,
            "used_bytes": 111,
            "total_reserved_bytes": 550,
            "fresh_available_bytes": 363,
            "checkpoint_validated": True,
            "checkpoint_remaining_bytes": 200,
            "other_reserved_bytes": 350,
            "available_bytes": 563,
        }

        terminal = snapshot(
            checkpoint_session_id=partial.upload_session_id,
            checkpoint_collection_id=partial.collection_id,
        )
        assert terminal.status_code == 200
        assert terminal.json()["matter_capacity"] == {
            **ordinary.json()["matter_capacity"],
            "checkpoint_validated": True,
        }

        for session_id, collection_id in (
            (foreign.upload_session_id, foreign.collection_id),
            (foreign_actor.upload_session_id, foreign_actor.collection_id),
            ("upload-session-" + "f" * 32, "source-collection-" + "f" * 32),
            (cancelled.upload_session_id, cancelled.collection_id),
            (checkpoint.upload_session_id, cancelled.collection_id),
        ):
            uncredited = snapshot(
                checkpoint_session_id=session_id,
                checkpoint_collection_id=collection_id,
            )
            assert uncredited.status_code == 200
            assert uncredited.json()["matter_capacity"] == ordinary.json()[
                "matter_capacity"
            ]

        for malformed in (
            {"checkpoint_session_id": checkpoint.upload_session_id},
            {"checkpoint_collection_id": checkpoint.collection_id},
            {
                "checkpoint_session_id": "upload-session-not-valid",
                "checkpoint_collection_id": checkpoint.collection_id,
            },
            {
                "checkpoint_session_id": checkpoint.upload_session_id,
                "checkpoint_collection_id": "source-collection-not-valid",
            },
        ):
            before = bench.workspace.pending_upload_bytes(matter.matter_id)
            response = snapshot(**malformed)
            assert response.status_code == 400
            assert response.json() == {
                "message": "The selected-file review request is invalid."
            }
            assert bench.workspace.pending_upload_bytes(matter.matter_id) == before


def test_selection_preflight_minimizes_unsafe_names_and_marks_repeated_paths(tmp_path):
    scanner = UnavailableCountingScanner()
    app = _app(tmp_path, scanner)
    with TestClient(app) as client:
        slug = _matter(client)
        escaped = "../private/secret.txt"
        response = client.post(
            f"/matters/{slug}/upload-preflight",
            json={
                "files": [
                    {
                        "name": "secret.txt",
                        "relative_path": escaped,
                        "size": 12,
                        "media_type": escaped,
                    },
                    {"name": "copy.txt", "relative_path": "Batch/record.txt", "size": 12},
                    {"name": "copy.txt", "relative_path": "batch/RECORD.txt", "size": 12},
                ]
            },
        )

        assert response.status_code == 200
        assert escaped not in response.text
        payload = response.json()
        assert [item["state"] for item in payload["items"]] == [
            "failed",
            "valid",
            "duplicate_candidate",
        ]
        assert payload["items"][0]["display_name"] == "Selected file 1"
        assert payload["items"][0]["path_safety_validated"] is False
        assert payload["items"][2]["duplicate"] == "selection_collision"
        assert payload["eligible_indexes"] == [1]
        assert scanner.scan_calls == 0


def test_selection_preflight_keeps_scan_policy_on_duplicate_and_over_limit_rows(tmp_path):
    scanner = UnavailableCountingScanner()
    app = _app(tmp_path, scanner)
    with TestClient(app) as client:
        slug = _matter(client)
        before_status_calls = scanner.status_calls
        response = client.post(
            f"/matters/{slug}/upload-preflight",
            json={
                "files": [
                    {"name": "scan.png", "relative_path": "Batch/scan.png", "size": 68},
                    {"name": "SCAN.png", "relative_path": "batch/SCAN.png", "size": 68},
                    {"name": "large.png", "relative_path": "large.png", "size": 129},
                ]
            },
        )

        assert response.status_code == 200
        payload = response.json()
        assert [item["state"] for item in payload["items"]] == [
            "needs_attention",
            "needs_attention",
            "over_limit",
        ]
        assert all(
            item["scan"]
            == {
                "required": True,
                "capability": "unavailable",
                "result": "not_run",
            }
            for item in payload["items"]
        )
        assert scanner.status_calls == before_status_calls + 1
        assert scanner.scan_calls == 0


def test_selection_preflight_chooses_first_otherwise_eligible_duplicate(tmp_path):
    scanner = ReadyCountingScanner()
    app = _app(tmp_path, scanner)
    relative_paths = [
        "Batch/Record.txt",
        "batch/record.TXT",
        "BATCH/RECORD.txt",
        "batch/Record.txt",
        "Other/Note.txt",
        "other/note.TXT",
    ]
    sizes = [129, 12, 13, 130, 0, 12]
    with TestClient(app) as client:
        slug = _matter(client)
        response = client.post(
            f"/matters/{slug}/upload-preflight",
            json={
                "selection_nonce": "b" * 32,
                "files": [
                    {
                        "name": path.rsplit("/", 1)[-1],
                        "relative_path": path,
                        "size": size,
                        "media_type": "text/plain",
                    }
                    for path, size in zip(relative_paths, sizes, strict=True)
                ],
            },
        )

    assert response.status_code == 200
    payload = response.json()
    assert [item["state"] for item in payload["items"]] == [
        "over_limit",
        "valid",
        "duplicate_candidate",
        "over_limit",
        "failed",
        "valid",
    ]
    assert payload["eligible_indexes"] == [1, 5]
    assert payload["counts"] == {
        "valid": 2,
        "needs_attention": 0,
        "unsupported": 0,
        "duplicate_candidate": 1,
        "over_limit": 2,
        "failed": 1,
    }
    assert all(path not in response.text for path in relative_paths)
    assert scanner.scan_calls == 0


def test_selection_preflight_attests_only_shared_validator_safe_paths(tmp_path):
    scanner = UnavailableCountingScanner()
    app = _app(tmp_path, scanner)
    safe_paths = [
        "Folder A/archive.zip",
        "Folder B/archive.zip",
        "Folder A/empty.txt",
        "Folder B/missing-size.txt",
        "Folder A/oversize.txt",
    ]
    unsafe_paths = [
        "/private/evidence.txt",
        "../private/evidence.txt",
        "Folder/\x00evidence.txt",
        "Folder/\u202eevidence.txt",
        "CON/evidence.txt",
    ]
    with TestClient(app) as client:
        slug = _matter(client)
        response = client.post(
            f"/matters/{slug}/upload-preflight",
            json={
                "selection_nonce": "a" * 32,
                "files": [
                    {
                        "name": "archive.zip",
                        "relative_path": safe_paths[0],
                        "size": 4,
                        "media_type": "application/zip",
                    },
                    {
                        "name": "archive.zip",
                        "relative_path": safe_paths[1],
                        "size": 4,
                        "media_type": "application/zip",
                    },
                    {
                        "name": "empty.txt",
                        "relative_path": safe_paths[2],
                        "size": 0,
                        "media_type": "text/plain",
                    },
                    {
                        "name": "missing-size.txt",
                        "relative_path": safe_paths[3],
                        "media_type": "text/plain",
                    },
                    {
                        "name": "oversize.txt",
                        "relative_path": safe_paths[4],
                        "size": 129,
                        "media_type": "text/plain",
                    },
                    *[
                        {
                            "name": "evidence.txt",
                            "relative_path": path,
                            "size": 12,
                            "media_type": "text/plain",
                        }
                        for path in unsafe_paths
                    ],
                ],
            },
        )

    assert response.status_code == 200
    payload = response.json()
    assert [item["state"] for item in payload["items"][:5]] == [
        "unsupported",
        "unsupported",
        "failed",
        "failed",
        "over_limit",
    ]
    assert [item["path_safety_validated"] for item in payload["items"]] == [
        True,
        True,
        True,
        True,
        True,
        False,
        False,
        False,
        False,
        False,
    ]
    assert all(path not in response.text for path in safe_paths + unsafe_paths)
    assert scanner.scan_calls == 0


def test_selection_preflight_is_matter_scoped_and_never_claims_a_completed_scan(tmp_path):
    scanner = ReadyCountingScanner()
    app = _app(tmp_path, scanner)
    with TestClient(app) as client:
        slug = _matter(client)
        other = client.app.state.workbench.create_matter(
            "Other synthetic matter",
            "Isolation canary",
            "development-jordan-lee",
        )

        before_hidden_status_calls = scanner.status_calls
        hidden = client.post(
            f"/matters/{other.slug}/upload-preflight",
            json={"files": [{"name": "scan.png", "size": 68}]},
        )
        assert scanner.status_calls == before_hidden_status_calls
        before_allowed_status_calls = scanner.status_calls
        allowed = client.post(
            f"/matters/{slug}/upload-preflight",
            json={
                "files": [
                    {
                        "name": "scan.png",
                        "relative_path": "scan.png",
                        "size": 68,
                        "media_type": "image/png",
                    }
                ]
            },
        )

        assert hidden.status_code == 404
        assert allowed.status_code == 200
        item = allowed.json()["items"][0]
        assert item["state"] == "valid"
        assert item["scan"] == {
            "required": True,
            "capability": "ready",
            "result": "not_run",
        }
        assert item["detected_type"] is None
        assert item["source_version"] == "pending_upload"
        assert item["duplicate"] == "not_evaluated"
        assert scanner.status_calls == before_allowed_status_calls + 1
        assert scanner.scan_calls == 0


def test_selection_preflight_fails_closed_for_empty_or_malformed_entries(tmp_path):
    scanner = UnavailableCountingScanner()
    app = _app(tmp_path, scanner)
    with TestClient(app) as client:
        slug = _matter(client)
        response = client.post(
            f"/matters/{slug}/upload-preflight",
            json={
                "files": [
                    {"name": "empty.txt", "size": 0},
                    {"name": "missing-size.txt"},
                    ["not", "an", "object"],
                ]
            },
        )

        assert response.status_code == 200
        payload = response.json()
        assert payload["status"] == "blocked"
        assert payload["counts"]["failed"] == 3
        assert payload["eligible_indexes"] == []
        assert all(item["eligible"] is False for item in payload["items"])
        assert scanner.scan_calls == 0


def test_setup_exposes_review_before_upload_and_no_script_fallback(tmp_path):
    scanner = UnavailableCountingScanner()
    app = _app(tmp_path, scanner)
    with TestClient(app) as client:
        slug = _matter(client)
        response = client.get(f"/matters/{slug}/setup")

    assert response.status_code == 200
    assert f'data-preflight-url="/matters/{slug}/upload-preflight"' in response.text
    assert f'data-max-preflight-request-bytes="{PREFLIGHT_REQUEST_BYTES}"' in response.text
    assert 'data-upload-preflight' in response.text
    assert 'data-upload-preflight-items' in response.text
    assert 'data-upload-preflight-confirm' in response.text
    folder_input = re.search(r'<input\b[^>]*\bid="source-folder"[^>]*>', response.text)
    assert folder_input is not None
    folder_input_markup = folder_input.group(0)
    assert " hidden" in folder_input_markup
    assert " disabled" in folder_input_markup
    assert 'tabindex="-1"' in folder_input_markup
    assert 'class="button button-primary" data-folder-chooser hidden' in response.text
    assert 'data-upload-preflight-cancel>Cancel selection' in response.text
    collection_input = re.search(r'<input\b[^>]*\bid="upload-collection-name"[^>]*>', response.text).group(0)
    assert " required" in collection_input
    assert 'value="Uploaded sources"' not in collection_input
    assert "Review selected files" in response.text
    assert "Upload 0 ready files" in response.text
    assert "Without JavaScript, the retained direct upload is limited to 1–10 files per request." in response.text
    assert "Turn on JavaScript to review up to 10,000 selected records in resumable batches." in response.text


def test_no_script_direct_upload_rejects_eleven_files_without_durable_state(tmp_path):
    scanner = UnavailableCountingScanner()
    app = _app(tmp_path, scanner)
    with TestClient(app) as client:
        slug = _matter(client)
        bench = client.app.state.workbench
        matter = bench.matter(slug, ACTOR)
        store = bench.source_store(matter)
        before_documents = tuple(store.documents)
        before_collections = bench.workspace.source_collections(matter.matter_id)
        before_sessions = bench.workspace.recent_upload_sessions(matter.matter_id, ACTOR)
        before_reserved = bench.workspace.pending_upload_bytes(matter.matter_id)

        response = client.post(
            f"/matters/{slug}/uploads",
            files=[
                ("files", (f"record-{index:02d}.txt", b"x", "text/plain"))
                for index in range(11)
            ],
            follow_redirects=False,
        )

        assert response.status_code == 303
        assert "Choose+between+1+and+10+files+for+each+upload." in response.headers[
            "location"
        ]
        assert tuple(store.documents) == before_documents
        assert bench.workspace.source_collections(matter.matter_id) == before_collections
        assert bench.workspace.recent_upload_sessions(matter.matter_id, ACTOR) == before_sessions
        assert bench.workspace.pending_upload_bytes(matter.matter_id) == before_reserved
        assert scanner.scan_calls == 0


def test_direct_upload_preserves_explicit_collection_name(tmp_path):
    app = _app(tmp_path, UnavailableCountingScanner())
    with TestClient(app) as client:
        slug = _matter(client)
        response = client.post(f"/matters/{slug}/uploads",
            data={"collection_name": "Synthetic named collection"},
            files={"files": ("synthetic.txt", b"Synthetic source", "text/plain")},
            follow_redirects=False)
        assert response.status_code == 303
        page = client.get(response.headers["location"])
        assert "Synthetic named collection" in page.text


def test_direct_upload_rejects_whitespace_collection_before_storage(tmp_path):
    app = _app(tmp_path, UnavailableCountingScanner())
    with TestClient(app) as client:
        slug = _matter(client)
        response = client.post(f"/matters/{slug}/uploads",
            data={"collection_name": "   "},
            files={"files": ("synthetic.txt", b"Synthetic source", "text/plain")})
        assert response.status_code == 400
        matter = app.state.workbench.workspace.get_active_matter(slug)
        assert not app.state.workbench.workspace.source_collections(matter.matter_id)
