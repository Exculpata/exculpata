"""Synthetic authorization and response-lifecycle checks for rendered clips."""

from tests.fixture_write import fixture_write

from case_intelligence.legacy_names import (
    LEGACY_STEM,
)
import asyncio
import io
import threading

import pytest
from fastapi.testclient import TestClient

from case_intelligence import workbench as module
from case_intelligence.pilot_uploads import _MediaProbe
from tests.test_full_text_response_lifecycle import asgi_download
from tests.test_matter_management import ADMIN, OTHER, OWNER, _app, _headers, _principal_id
from tests.test_report_review_basis import ACTOR, workspace


def seed_clip(bench, matter, actor, monkeypatch):
    # Codec execution is outside these HTTP-boundary tests. Use synthetic bytes
    # and the normal source store; the saved clip is an existing valid interval.
    monkeypatch.setattr("case_intelligence.pilot_uploads._probe_media",
                        lambda *args: _MediaProbe(2000, False, True))
    store = bench.source_store(matter)
    document, _ = store.store_stream("synthetic.wav", "audio/wav",
                                    io.BytesIO(b"RIFF synthetic audio"))
    bench.media.close()
    with fixture_write(bench.workspace, media=True):
        job = bench.workspace.queue_media_job(
            matter.matter_id, document.document_id, document.version_id, actor,
            source_sha256=document.digest, byte_size=document.size, media_type="audio/wav", duration_ms=2000)
        assert bench.workspace.claim_media_job("synthetic-clip-worker").media_job_id == job.media_job_id
        bench.workspace.import_media_transcript(job.media_job_id, segments=({
            "external_segment_id": "synthetic-segment", "start_ms": 0, "end_ms": 2000,
            "speaker_cluster": "SPEAKER_00", "model_text": "Synthetic recording.",
        },), warnings=(), quality={}, provenance={})
        bench.workspace.finish_media_job(job.media_job_id, degraded=False, message="Synthetic ready")
        clip = bench.workspace.create_media_clip(matter.matter_id, document.document_id, document.version_id,
            title="Synthetic interval", start_ms=0, end_ms=2000, actor_id=actor)
    return f"/matters/{matter.slug}/sources/{store.action_token(document)}/clips/{clip.clip_id}/download"



@pytest.mark.parametrize("revocation", ["membership", "session", "principal", "administrator"])
def test_clip_rechecks_authority_after_render(tmp_path, monkeypatch, revocation):
    monkeypatch.setenv("CASE_INTELLIGENCE_STORAGE_RESERVE_GIB", "0")
    with TestClient(_app(tmp_path), base_url=f"https://{LEGACY_STEM}.example.test") as client:
        client.get("/auth/login", headers=_headers(OWNER))
        bench = client.app.state.workbench
        owner = _principal_id(client, OWNER)
        matter = bench.workspace.create_matter("Synthetic clip access", "Synthetic", owner)
        path = seed_clip(bench, matter, owner, monkeypatch)
        subject = ADMIN if revocation == "administrator" else OTHER
        client.cookies.clear()
        client.get("/auth/login", headers=_headers(subject))
        reader = _principal_id(client, subject)
        if revocation != "administrator":
            bench.workspace.add_member(matter.matter_id, reader, owner)
        rendered = []

        def render(source, clip, *, has_video, output):
            output.write_bytes(b"RIFF synthetic confidential clip")
            rendered.append(output)
            if revocation == "membership":
                bench.workspace.revoke_member(matter.matter_id, reader, owner)
            elif revocation == "administrator":
                monkeypatch.setattr(client.app.state.identity, "kerberos_group_resolver", lambda _: set())
            else:
                with fixture_write(bench.workspace):
                    if revocation == "session":
                        bench.workspace.connection.execute(
                            "UPDATE workbench_session SET revoked_at=? WHERE principal_id=?",
                            (bench.workspace._now(), reader))
                    else:
                        bench.workspace.connection.execute(
                            "UPDATE workbench_principal SET active=0 WHERE principal_id=?", (reader,))

        monkeypatch.setattr(module, "render_clip", render)
        response = client.get(path, headers=_headers(subject))
        assert response.status_code == 404
        assert b"synthetic confidential clip" not in response.content
        assert rendered and not rendered[0].parent.exists()
        assert bench._active_clip_export_count(matter.matter_id) == 0
        assert not any(event.action == "media.clip_export" and event.outcome == "success"
                       for event in bench.workspace.audit_events(matter.matter_id))


@pytest.mark.parametrize("failure", ["start", "body", "cancel", "disconnect", "render", "response", "audit"])
def test_failed_clip_download_removes_render_and_releases_admission(workspace, monkeypatch, failure):
    client, bench, matter = workspace
    path = seed_clip(bench, matter, ACTOR, monkeypatch)
    rendered = []

    def render(source, clip, *, has_video, output):
        output.write_bytes(b"RIFF synthetic clip" * 10000)
        rendered.append(output)
        if failure == "render":
            raise OSError("Synthetic render failure")

    monkeypatch.setattr(module, "render_clip", render)
    if failure == "response":
        def broken_response(*args, **kwargs):
            raise RuntimeError("Synthetic response construction failure")
        monkeypatch.setattr(module, "FileResponse", broken_response)
    if failure == "audit":
        original = bench.workspace.append_audit_event
        def broken_audit(**kwargs):
            if kwargs["action"] == "media.clip_export":
                raise OSError("Synthetic audit failure")
            return original(**kwargs)
        monkeypatch.setattr(bench.workspace, "append_audit_event", broken_audit)

    async def send(message):
        if failure == "cancel" and message["type"] == "http.response.body":
            raise asyncio.CancelledError()
        if (failure == "start" and message["type"] == "http.response.start") or (
            failure == "body" and message["type"] == "http.response.body" and message.get("body")
        ):
            raise OSError("Synthetic disconnected client")

    request = asgi_download(client.app, path, send_hook=send, disconnect=failure == "disconnect")
    if failure == "disconnect":
        asyncio.run(request)
    else:
        with pytest.raises(asyncio.CancelledError if failure == "cancel" else Exception):
            asyncio.run(request)
    assert rendered and not rendered[0].parent.exists()
    assert bench._active_clip_export_count(matter.matter_id) == 0


def test_concurrent_clip_downloads_release_only_their_own_artifacts(workspace, monkeypatch):
    client, bench, matter = workspace
    path = seed_clip(bench, matter, ACTOR, monkeypatch)
    rendered = []

    def render(source, clip, *, has_video, output):
        output.write_bytes(b"RIFF synthetic clip")
        rendered.append(output)
    monkeypatch.setattr(module, "render_clip", render)

    async def download_both():
        second_started = asyncio.Event()
        release_second = asyncio.Event()

        async def first_send(message):
            if message["type"] == "http.response.start":
                await asyncio.wait_for(second_started.wait(), 5)
                assert bench._active_clip_export_count(matter.matter_id) == 2
                assert all(output.exists() for output in rendered)

        async def second_send(message):
            if message["type"] == "http.response.start":
                second_started.set()
                await asyncio.wait_for(release_second.wait(), 5)

        first = asyncio.create_task(asgi_download(client.app, path, send_hook=first_send))
        second = asyncio.create_task(asgi_download(client.app, path, send_hook=second_send))
        try:
            await first
            assert bench._active_clip_export_count(matter.matter_id) == 1
            assert sum(output.exists() for output in rendered) == 1
        finally:
            release_second.set()
            await second

    asyncio.run(download_both())
    assert len(rendered) == 2 and all(not output.parent.exists() for output in rendered)
    assert bench._active_clip_export_count(matter.matter_id) == 0


def test_cancellation_during_render_keeps_files_until_worker_exits(workspace, monkeypatch):
    client, bench, matter = workspace
    path = seed_clip(bench, matter, ACTOR, monkeypatch)
    started = threading.Event()
    release_renderer = threading.Event()
    rendered = []
    renderer_reads = []

    def render(source, clip, *, has_video, output):
        output.write_bytes(b"RIFF synthetic in-use clip")
        rendered.append(output)
        started.set()
        assert release_renderer.wait(5)
        renderer_reads.append(output.read_bytes())
    monkeypatch.setattr(module, "render_clip", render)

    async def cancel_during_render():
        request = asyncio.create_task(asgi_download(client.app, path))
        try:
            assert await asyncio.to_thread(started.wait, 5)
            request.cancel()
            # Let cancellation reach the ASGI stack while the render is held.
            await asyncio.sleep(0.05)
            assert rendered[0].exists()
            assert bench._active_clip_export_count(matter.matter_id) == 1
        finally:
            release_renderer.set()
            with pytest.raises(asyncio.CancelledError):
                await request

    asyncio.run(cancel_during_render())
    assert renderer_reads == [b"RIFF synthetic in-use clip"]
    assert not rendered[0].parent.exists()
    assert bench._active_clip_export_count(matter.matter_id) == 0


def test_successful_clip_retains_admission_through_final_send(workspace, monkeypatch):
    client, bench, matter = workspace
    path = seed_clip(bench, matter, ACTOR, monkeypatch)
    rendered = []

    def render(source, clip, *, has_video, output):
        output.write_bytes(b"RIFF synthetic clip")
        rendered.append(output)
    monkeypatch.setattr(module, "render_clip", render)

    async def send(message):
        assert bench._active_clip_export_count(matter.matter_id) == 1
        assert rendered[0].exists()

    messages = asyncio.run(asgi_download(client.app, path, send_hook=send))
    assert messages[0]["status"] == 200
    assert b"RIFF synthetic clip" == b"".join(item.get("body", b"") for item in messages)
    assert not rendered[0].parent.exists()
    assert bench._active_clip_export_count(matter.matter_id) == 0
