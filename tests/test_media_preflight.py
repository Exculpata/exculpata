"""Synthetic pre-transcription inspection contracts, separate from frozen packs."""

from tests.fixture_write import fixture_write
import subprocess
import hashlib
import zipfile
import os
import shutil
import sys
import threading
import time
import wave
import pytest
from pathlib import Path

from case_intelligence.media_preflight import inspect_recording
from case_intelligence.pilot_uploads import _probe_media
from case_intelligence.generation import UnavailableGenerator
from case_intelligence.matter_archive_verifier import verify_matter_archive
from case_intelligence.workbench import create_workbench_app
from fastapi.testclient import TestClient
from tests.test_matter_media_workflow import ACTOR, ImmediateMediaProcessor, _matter
from tests.case_lifecycle_web_helpers import prepare_preview_exit, preview_login

SPEECH = Path(__file__).parent / "fixtures/media-preflight/generated-speech.wav"


def silence(path: Path, seconds=2):
    with wave.open(str(path), "wb") as output:
        output.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        output.writeframes(b"\x00\x00" * 16000 * seconds)


def test_silence_is_reviewable_and_language_is_unknown(tmp_path):
    source = tmp_path / "silence.wav"
    silence(source)
    result = inspect_recording(source, "audio/wav")
    assert result["outcome"] == "no_speech"
    assert result["first_speech_ms"] is None
    assert result["last_speech_ms"] is None
    assert result["language"] == "not_assessed"
    assert result["complete"] is True
    assert result["quiet_ms"] >= 1980


def test_no_audio_video_is_admitted_for_playback(tmp_path):
    source = tmp_path / "silent-video.mp4"
    subprocess.run(["/usr/bin/ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                    "color=c=blue:s=160x120:d=1", "-an", "-c:v", "libx264",
                    "-pix_fmt", "yuv420p", str(source)], check=True)
    assert _probe_media(source, "video/mp4").browser_compatible
    result = inspect_recording(source, "video/mp4")
    assert result["outcome"] == "no_audio"
    assert result["complete"] is True


def test_failed_inspection_is_not_no_speech(tmp_path):
    source = tmp_path / "broken.wav"
    source.write_bytes(b"not a recording")
    assert inspect_recording(source, "audio/wav")["outcome"] == "failed"


def test_original_time_after_leading_and_interior_silence(tmp_path):
    source = tmp_path / "late-speech.wav"
    with wave.open(str(SPEECH), "rb") as speech:
        data = speech.readframes(speech.getnframes())
    with wave.open(str(source), "wb") as output:
        output.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        output.writeframes(b"\0\0" * 16000 * 30 + data + b"\0\0" * 16000 * 3 + data + b"\0\0" * 16000 * 2)
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    result = inspect_recording(source, "audio/wav")
    assert result["outcome"] == "ready"
    assert 30_000 <= result["first_speech_ms"] < 31_000
    assert 43_000 <= result["last_speech_ms"] < 45_000
    assert result["leading_quiet_ms"] >= 30_000
    assert result["trailing_quiet_ms"] >= 1_900
    assert result["quiet_ms"] >= 35_000
    assert hashlib.sha256(source.read_bytes()).hexdigest() == before


def test_unavailable_detector_and_partial_check_are_uncertain(tmp_path, monkeypatch):
    import case_intelligence.media_preflight as module
    monkeypatch.setitem(sys.modules, "webrtcvad", None)
    assert inspect_recording(SPEECH, "audio/wav")["outcome"] == "uncertain"
    monkeypatch.undo()
    monkeypatch.setattr(module, "MAX_SECONDS", 1)
    result = inspect_recording(SPEECH, "audio/wav")
    assert result["outcome"] == "uncertain"
    assert not result["complete"]
    assert result["checked_ms"] <= 1_000


def test_noise_clipping_and_low_volume_remain_reviewable(tmp_path):
    for name, expression in (
        ("noise", "anoisesrc=d=3:c=white:a=0.1:seed=42"),
        ("clipping", "aevalsrc=0.99*sgn(sin(2*PI*440*t)):d=3"),
    ):
        source = tmp_path / f"{name}.wav"
        subprocess.run(["/usr/bin/ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                        expression, "-ar", "16000", "-ac", "1", str(source)], check=True)
        result = inspect_recording(source, "audio/wav")
        assert result["outcome"] == "uncertain"
        if name == "clipping":
            assert "clipping" in result["quality"]
    source = tmp_path / "quiet-speech.wav"
    subprocess.run(["/usr/bin/ffmpeg", "-v", "error", "-i", str(SPEECH),
                    "-af", "volume=0.001", str(source)], check=True)
    result = inspect_recording(source, "audio/wav")
    assert result["outcome"] == "uncertain"
    assert "low_volume" in result["quality"]


def test_multiple_audio_tracks_cannot_claim_a_complete_check(tmp_path):
    quiet = tmp_path / "quiet.wav"
    silence(quiet, 8)
    source = tmp_path / "two-audio-tracks.mp4"
    subprocess.run(["/usr/bin/ffmpeg", "-v", "error", "-i", str(quiet), "-i", str(SPEECH),
                    "-f", "lavfi", "-i", "color=c=blue:s=160x120:d=5",
                    "-map", "2:v", "-map", "0:a", "-map", "1:a", "-c:v", "libx264",
                    "-c:a", "aac", "-shortest", str(source)], check=True)
    result = inspect_recording(source, "video/mp4")
    assert result["outcome"] == "uncertain"
    assert not result["complete"]
    assert "multiple_audio_tracks" in result["quality"]


class ObservedProcessor(ImmediateMediaProcessor):
    def __init__(self):
        super().__init__()
        self.readiness_calls = 0
        self.input_digest = ""

    def ready(self, owner):
        self.readiness_calls += 1
        return True

    def submit(self, owner, source, media_type, source_sha256):
        self.input_digest = hashlib.sha256(source.read_bytes()).hexdigest()
        return super().submit(owner, source, media_type, source_sha256)


def app_for(path, processor, *, background=False, auth_mode="test"):
    return create_workbench_app(path, generator=UnavailableGenerator(), auth_mode=auth_mode,
                                media_processor=processor, media_poll_seconds=0.01, background_ingestion=background)


def upload(client, slug, source, media_type="audio/wav"):
    response = client.post(f"/matters/{slug}/uploads", files=[
        ("files", (source.name, source.read_bytes(), media_type))], follow_redirects=False)
    assert response.status_code == 303, response.text
    bench = client.app.state.workbench
    matter = bench.matter(slug, ACTOR)
    store = bench.source_store(matter)
    document = next(iter(store.documents.values()))
    token = store.action_token(document)
    return bench, matter, document, token


def wait_job(bench, matter, document, states=("cancelled",)):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        job = bench.workspace.media_job(matter.matter_id, document.document_id, document.version_id)
        if job and job.state in states:
            return job
        time.sleep(0.02)
    raise AssertionError(f"Media did not reach {states}: {job}")


def test_review_survives_restart_then_explicit_continue_submits_once(tmp_path):
    source = tmp_path / "silence.wav"
    silence(source, 8)
    processor = ObservedProcessor()
    runtime = tmp_path / "runtime"
    with TestClient(app_for(runtime, processor)) as client:
        slug = _matter(client)
        bench, matter, document, token = upload(client, slug, source)
        job = wait_job(bench, matter, document)
        assert job.preflight["outcome"] == "no_speech"
        assert processor.readiness_calls == processor.submissions == 0
        assert document.state == "needs_review"
        assert not bench.source_store(matter).ready_documents()
        inspection = job.preflight["inspection_id"]
        upload(client, slug, source)
        assert len(bench.workspace.media_jobs_for_matter(matter.matter_id)) == 1
        assert wait_job(bench, matter, document).preflight["inspection_id"] == inspection
        page = client.get(f"/matters/{slug}/sources/{token}")
        assert page.status_code == 200
        assert "No speech detected" in page.text
        assert "Transcribe this recording" in page.text
        content = client.get(f"/matters/{slug}/sources/{token}/content", headers={"Range": "bytes=0-15"})
        assert content.status_code == 206
        assert content.content == source.read_bytes()[:16]
        foreign = _matter(client, "Separate synthetic matter")
        assert client.get(f"/matters/{foreign}/sources/{token}").status_code == 404
        assert client.post(f"/matters/{foreign}/sources/{token}/recording-check",
                           data={"action": "continue", "inspection_id": inspection}).status_code == 404
    with TestClient(app_for(runtime, processor)) as client:
        bench = client.app.state.workbench
        matter = bench.matter(slug, ACTOR)
        document = bench.source_store(matter).get_by_action_token(token)
        assert wait_job(bench, matter, document).preflight["inspection_id"] == inspection
        assert processor.submissions == 0
        decision_url = f"/matters/{slug}/sources/{token}/recording-check"
        data = {"action": "continue", "inspection_id": inspection}
        assert client.post(decision_url, data=data, follow_redirects=False).status_code == 303
        assert client.post(decision_url, data=data, follow_redirects=False).status_code == 409
        finished = wait_job(bench, matter, document, ("succeeded", "degraded"))
        assert finished.preflight["continued"] is True
        assert processor.submissions == 1
        assert processor.input_digest == hashlib.sha256(source.read_bytes()).hexdigest()


def test_changed_source_cannot_reuse_review_decision(tmp_path):
    source = tmp_path / "silence.wav"
    silence(source, 8)
    processor = ObservedProcessor()
    with TestClient(app_for(tmp_path / "runtime", processor)) as client:
        slug = _matter(client)
        bench, matter, document, token = upload(client, slug, source)
        job = wait_job(bench, matter, document)
        stored = bench.source_store(matter).source_path(document.document_id)
        metadata = stored.stat()
        body = bytearray(stored.read_bytes())
        body[-1] = 1
        stored.write_bytes(body)
        os.utime(stored, ns=(metadata.st_atime_ns, metadata.st_mtime_ns))
        response = client.post(f"/matters/{slug}/sources/{token}/recording-check",
                               data={"action": "continue", "inspection_id": job.preflight["inspection_id"]})
        assert response.status_code == 409
        assert processor.submissions == 0


def test_no_audio_video_plays_without_processor_and_is_not_searchable(tmp_path):
    source = tmp_path / "silent-video.mov"
    subprocess.run(["/usr/bin/ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                    "color=c=blue:s=160x120:d=1", "-an", "-c:v", "mpeg4",
                    str(source)], check=True)
    processor = ObservedProcessor()
    with TestClient(app_for(tmp_path / "runtime", processor, auth_mode="preview")) as client:
        preview_login(client)
        slug = _matter(client)
        bench, matter, document, token = upload(client, slug, source, "video/quicktime")
        job = wait_job(bench, matter, document)
        assert job.display_state == "playback_only"
        deadline = time.monotonic() + 10
        while document.playback_state not in {"ready", "failed"} and time.monotonic() < deadline:
            time.sleep(0.02)
        assert document.playback_state == "ready", document.playback_message
        assert document.state == "playback_only"
        assert processor.readiness_calls == processor.submissions == 0
        page = client.get(f"/matters/{slug}/sources/{token}")
        assert page.status_code == 200
        assert "Video without audio" in page.text
        assert "Sources available for review" in page.text
        assert "Retry or remove an affected source" not in page.text
        assert "Transcribe this recording" not in page.text
        content = client.get(f"/matters/{slug}/sources/{token}/content", headers={"Range": "bytes=0-63"})
        assert content.status_code == 206
        assert "video/mp4" in content.headers["content-type"]
        assert client.get(f"/matters/{slug}/sources/{token}/media-status").json()["ready"] is False
        assert client.post(f"/matters/{slug}/sources/{token}/recording-check", data={
            "action": "continue", "inspection_id": job.preflight["inspection_id"]}).status_code == 409
        assert not bench.source_store(matter).ready_documents()
        readiness = bench.workspace.matter_readiness(matter.matter_id)
        assert readiness.playback_only_count == 1
        assert readiness.searchable_count == 0
        assert not readiness.can_query
        confirmation = prepare_preview_exit(client, matter)
        current_exit = bench.workspace.case_lifecycle.current_exit(matter.matter_id)
        preservation = client.get(f"/matters/{slug}/close/export")
        assert preservation.status_code == 200
        archive_path = tmp_path / "generated-playback-preservation.zip"
        archive_path.write_bytes(preservation.content)
        manifest = verify_matter_archive(archive_path, expected_sha256=current_exit.archive_digest)
        assert manifest["table_rows"]["workbench_media_job"] == 1
        with zipfile.ZipFile(archive_path) as archive:
            assert archive.read("originals/" + document.document_id) == source.read_bytes()
            assert "playback/" + document.document_id + ".mp4" in archive.namelist()
        closed = client.post(f"/matters/{slug}/close", data=confirmation, follow_redirects=False)
        assert closed.status_code == 303
        assert closed.headers["location"].startswith("/matters/new?")
        assert bench.workspace.matter_lifecycle(matter.matter_id).state == "deleted"
        assert bench.workspace.connection.execute(
            "SELECT COUNT(*) FROM workbench_media_job WHERE matter_id=?", (matter.matter_id,)
        ).fetchone()[0] == 0
        assert source.is_file()


def test_failed_check_retry_invalidates_prior_decision(tmp_path, monkeypatch):
    import case_intelligence.media_evidence as module
    source = tmp_path / "silence.wav"
    silence(source, 8)
    failed = inspect_recording(tmp_path / "absent.wav", "audio/wav")
    monkeypatch.setattr(module, "inspect_recording", lambda *a, **kw: dict(failed))
    processor = ObservedProcessor()
    with TestClient(app_for(tmp_path / "runtime", processor)) as client:
        slug = _matter(client)
        bench, matter, document, token = upload(client, slug, source)
        job = wait_job(bench, matter, document)
        old_id = job.preflight["inspection_id"]
        assert job.preflight["outcome"] == "failed"
        url = f"/matters/{slug}/sources/{token}/recording-check"
        assert client.post(url, data={"action": "continue", "inspection_id": old_id}).status_code == 409
        assert processor.readiness_calls == 0
        monkeypatch.undo()
        assert client.post(url, data={"action": "retry", "inspection_id": old_id}, follow_redirects=False).status_code == 303
        checked = wait_job(bench, matter, document)
        assert checked.preflight["outcome"] == "no_speech"
        assert checked.preflight["inspection_id"] != old_id
        assert client.post(url, data={"action": "continue", "inspection_id": old_id}).status_code == 409
        assert processor.submissions == 0


def test_interrupted_inspection_recovers_and_backup_restores_hold(tmp_path, monkeypatch):
    import case_intelligence.media_evidence as module
    source = tmp_path / "silence.wav"
    silence(source, 8)
    entered = threading.Event()

    def interrupted(source, media_type, *, cancelled):
        entered.set()
        deadline = time.monotonic() + 10
        while not cancelled() and time.monotonic() < deadline:
            time.sleep(0.01)
        return {"outcome": "uncertain"}

    monkeypatch.setattr(module, "inspect_recording", interrupted)
    processor = ObservedProcessor()
    runtime = tmp_path / "runtime"
    with TestClient(app_for(runtime, processor)) as client:
        slug = _matter(client)
        bench, matter, document, token = upload(client, slug, source)
        assert entered.wait(3)
        assert processor.submissions == 0
    monkeypatch.undo()
    with TestClient(app_for(runtime, processor)) as client:
        bench = client.app.state.workbench
        matter = bench.matter(slug, ACTOR)
        document = bench.source_store(matter).get_by_action_token(token)
        job = wait_job(bench, matter, document)
        assert job.preflight["outcome"] == "no_speech"
        assert job.attempts == 2
        inspection_id = job.preflight["inspection_id"]
    restored = tmp_path / "restored"
    shutil.copytree(runtime, restored)  # stopped synthetic whole boundary, no external originals
    with TestClient(app_for(restored, processor)) as client:
        bench = client.app.state.workbench
        matter = bench.matter(slug, ACTOR)
        document = bench.source_store(matter).get_by_action_token(token)
        job = wait_job(bench, matter, document)
        assert job.preflight["inspection_id"] == inspection_id
        assert processor.submissions == 0
        assert client.get(f"/matters/{slug}/sources/{token}/content").content == source.read_bytes()


def test_late_speech_keeps_transcript_citations_and_exports_on_original_time(tmp_path):
    class OriginalTimeProcessor(ObservedProcessor):
        def transcript(self, owner, external_job_id):
            payload = dict(super().transcript(owner, external_job_id))
            for segment in payload["segments"]:
                segment["start"] += 30
                segment["end"] += 30
            payload["quality"]["preflight"] = {"outcome": "no_audio"}
            return payload

    source = tmp_path / "late.wav"
    with wave.open(str(SPEECH), "rb") as speech:
        data = speech.readframes(speech.getnframes())
    with wave.open(str(source), "wb") as output:
        output.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        output.writeframes(b"\0\0" * 16000 * 30 + data + b"\0\0" * 16000 * 10)
    processor = OriginalTimeProcessor()
    with TestClient(app_for(tmp_path / "runtime", processor)) as client:
        slug = _matter(client)
        bench, matter, document, token = upload(client, slug, source)
        job = wait_job(bench, matter, document, ("succeeded", "degraded"))
        assert job.preflight["outcome"] == "ready"  # processor cannot overwrite app inspection
        assert job.preflight["first_speech_ms"] >= 30_000
        assert processor.input_digest == hashlib.sha256(source.read_bytes()).hexdigest()
        unit = document.parsed_units()[0]
        assert unit.start_ms == 30_000 and unit.end_ms == 32_000
        candidate = bench._candidate(matter, document, unit, 1)
        support_token = bench._support_token(candidate)
        support = bench.support(matter, support_token)
        assert support.start_ms == 30_000 and support.end_ms == 32_000
        opened = client.get(f"/matters/{slug}?support={support_token}")
        assert opened.status_code == 200
        assert 'data-support-media data-start-ms="30000"' in opened.text
        exported = client.get(f"/matters/{slug}/sources/{token}/transcript-export?format=srt")
        assert exported.status_code == 200
        assert "00:00:30,000 --> 00:00:32,000" in exported.text


@pytest.mark.parametrize('outcome', ['no_audio', 'no_speech', 'uncertain', 'failed'])
def test_held_resumable_upload_finishes_processing(tmp_path, monkeypatch, outcome):
    import case_intelligence.media_evidence as module
    result = {'outcome': outcome, 'complete': True, 'quality': [], 'language': 'not_assessed'}
    monkeypatch.setattr(module, 'inspect_recording', lambda *a, **kw: dict(result))
    source = tmp_path / 'quiet.wav'
    silence(source)
    body = source.read_bytes()
    processor = ObservedProcessor()
    with TestClient(app_for(tmp_path / 'runtime', processor, background=True)) as client:
        slug = _matter(client)
        created = client.post(f'/matters/{slug}/upload-sessions', json={
            'collection_name': 'Synthetic recordings', 'files': [{
                'name': source.name, 'relative_path': source.name,
                'size': len(body), 'media_type': 'audio/wav'}]})
        assert created.status_code == 201
        item = created.json()['items'][0]
        assert client.put(item['chunk_url'], content=body, headers={
            'Content-Type': 'application/octet-stream', 'X-Upload-Offset': '0'}).status_code == 200
        finalized = client.post(item['finalize_url'])
        assert finalized.status_code == 200
        bench = client.app.state.workbench
        matter = bench.matter(slug, ACTOR)
        document = next(iter(bench.source_store(matter).documents.values()))
        wait_job(bench, matter, document)
        payload = client.get(finalized.json()['status_url']).json()
        compact = client.get(finalized.json()['status_url'] + '?compact=true').json()
        assert compact['work_complete'] is True
        assert compact['processing_count'] == 0
        assert compact['attention_count'] == (0 if outcome == 'no_audio' else 1)
        assert compact['playback_only_count'] == (1 if outcome == 'no_audio' else 0)
        assert payload['work_complete'] is True
        assert payload['processing_count'] == payload['ready_count'] == 0
        assert payload['attention_count'] == (0 if outcome == 'no_audio' else 1)
        assert payload['playback_only_count'] == (1 if outcome == 'no_audio' else 0)
        assert payload['items'][0]['work_state'] == ('playback_only' if outcome == 'no_audio' else 'attention')
        assert processor.submissions == 0


def test_recording_decision_remains_in_activity_after_navigation(tmp_path):
    source = tmp_path / 'quiet.wav'
    silence(source)
    with TestClient(app_for(tmp_path / 'runtime', ObservedProcessor())) as client:
        slug = _matter(client, 'Synthetic pending recording')
        bench, matter, document, _ = upload(client, slug, source)
        wait_job(bench, matter, document)
        page = client.get(f'/matters/{slug}')
        assert 'data-activity-badge >1</strong>' in page.text
        other = _matter(client, 'Synthetic other matter')
        assert client.get(f'/matters/{other}/home').status_code == 200
        activity = client.get(f'/activity?matter={other}')
        assert 'Synthetic pending recording' in activity.text
        assert 'data-attention-count="1"' in activity.text
        assert 'Needs review' in activity.text


def test_recording_decision_panel_is_actionable_before_javascript(tmp_path):
    source = tmp_path / 'quiet.wav'
    silence(source)
    with TestClient(app_for(tmp_path / 'runtime', ObservedProcessor())) as client:
        slug = _matter(client)
        bench, matter, document, _ = upload(client, slug, source)
        wait_job(bench, matter, document)
        page = client.get(f'/matters/{slug}')
        panel = page.text.split('data-media-activity-list>')[1].split('</section>')[0]
        assert 'media-activity-marker attention' in panel
        assert '>Review recording</a>' in panel
        assert 'processing-pulse' not in panel


@pytest.mark.parametrize('include_text', [False, True])
def test_playback_only_has_no_unresolved_activity(tmp_path, include_text):
    source = tmp_path / 'video-only.mp4'
    subprocess.run(['/usr/bin/ffmpeg', '-v', 'error', '-f', 'lavfi', '-i',
                    'color=c=blue:s=160x120:d=1', '-an', '-c:v', 'libx264',
                    '-pix_fmt', 'yuv420p', str(source)], check=True)
    with TestClient(app_for(tmp_path / 'runtime', ObservedProcessor())) as client:
        slug = _matter(client, 'Synthetic playback workspace')
        bench, matter, document, token = upload(client, slug, source, 'video/mp4')
        wait_job(bench, matter, document)
        if include_text:
            response = client.post(f'/matters/{slug}/uploads', files=[
                ('files', ('synthetic.txt', b'Synthetic meeting notes for review.', 'text/plain'))])
            assert response.status_code == 200
        activity = client.get(f'/activity?matter={slug}')
        assert 'data-attention-count="0"' in activity.text
        assert 'Playback Only' in activity.text
        page = client.get(f'/matters/{slug}')
        assert 'data-activity-badge hidden>0</strong>' in page.text
        assert 'Open recordings' in page.text
        home = client.get(f'/matters/{slug}/home')
        assert 'Resolve first' not in home.text
        assert '1 need attention' not in home.text
        other = _matter(client, 'Synthetic alternate workspace')
        away = client.get(f'/activity?matter={other}')
        assert 'Synthetic playback workspace' not in away.text
        assert client.get(f'/matters/{slug}/sources/{token}/content').status_code == 200
        assert document.state == 'playback_only'


def test_stale_recording_decisions_do_not_hash_source(tmp_path, monkeypatch):
    source = tmp_path / 'quiet.wav'
    silence(source)
    with TestClient(app_for(tmp_path / 'runtime', ObservedProcessor())) as client:
        slug = _matter(client)
        bench, matter, document, token = upload(client, slug, source)
        wait_job(bench, matter, document)
        store = bench.source_store(matter)
        original = store.source_path
        digests = []

        def observe(*args, **kwargs):
            if kwargs.get('verify_digest'):
                digests.append(True)
            return original(*args, **kwargs)

        monkeypatch.setattr(store, 'source_path', observe)
        for inspection in ('', 'stale-inspection'):
            response = client.post(f'/matters/{slug}/sources/{token}/recording-check',
                                   data={'action': 'retry', 'inspection_id': inspection})
            assert response.status_code == 409
        assert not digests


def test_recording_decision_keeps_request_loop_responsive(tmp_path, monkeypatch):
    import asyncio
    from contextlib import contextmanager
    source = tmp_path / 'quiet.wav'
    silence(source)
    app = app_for(tmp_path / 'runtime', ObservedProcessor())
    loop = None
    loop_thread = None
    timed_out = []
    lock_threads = []

    @app.middleware('http')
    async def capture_loop(request, call_next):
        nonlocal loop, loop_thread
        loop = asyncio.get_running_loop()
        loop_thread = threading.get_ident()
        return await call_next(request)

    with TestClient(app) as client:
        slug = _matter(client)
        bench, matter, document, token = upload(client, slug, source)
        job = wait_job(bench, matter, document)
        store = bench.source_store(matter)
        original_path = store.source_path
        original_guard = store.mutation_guard

        def checked_path(*args, **kwargs):
            if kwargs.get('verify_digest'):
                released = threading.Event()
                loop.call_soon_threadsafe(released.set)
                timed_out.append(not released.wait(1))
            return original_path(*args, **kwargs)

        @contextmanager
        def checked_guard():
            lock_threads.append(threading.get_ident())
            with original_guard():
                yield

        monkeypatch.setattr(store, 'source_path', checked_path)
        monkeypatch.setattr(store, 'mutation_guard', checked_guard)
        response = client.post(f'/matters/{slug}/sources/{token}/recording-check', data={
            'action': 'retry', 'inspection_id': job.preflight['inspection_id']}, follow_redirects=False)
        assert response.status_code == 303
        assert timed_out and not any(timed_out)
        assert lock_threads and loop_thread not in lock_threads


def test_duplicate_decisions_cannot_exhaust_staff_request_workers(tmp_path, monkeypatch):
    import anyio
    from concurrent.futures import ThreadPoolExecutor
    source = tmp_path / 'quiet.wav'
    silence(source)
    app = app_for(tmp_path / 'runtime', ObservedProcessor())
    entered = threading.Event()
    release = threading.Event()

    @app.middleware('http')
    async def one_shared_worker(request, call_next):
        # Constrain the ordinary ASGI pool to expose shared-budget starvation.
        anyio.to_thread.current_default_thread_limiter().total_tokens = 1
        return await call_next(request)

    with TestClient(app) as client:
        slug = _matter(client)
        bench, matter, document, token = upload(client, slug, source)
        job = wait_job(bench, matter, document)
        store = bench.source_store(matter)
        original = store.source_path

        def slow_digest(*args, **kwargs):
            if kwargs.get('verify_digest'):
                entered.set()
                assert release.wait(10), 'Synthetic digest was not released'
            return original(*args, **kwargs)

        monkeypatch.setattr(store, 'source_path', slow_digest)
        url = f'/matters/{slug}/sources/{token}/recording-check'
        data = {'action': 'retry', 'inspection_id': job.preflight['inspection_id']}
        with ThreadPoolExecutor(max_workers=3) as callers:
            first = callers.submit(client.post, url, data=data, follow_redirects=False)
            try:
                assert entered.wait(3)
                duplicate = callers.submit(client.post, url, data=data, follow_redirects=False)
                assert duplicate.result(timeout=2).status_code == 409
                unrelated = callers.submit(client.get, '/matters/new')
                assert unrelated.result(timeout=2).status_code == 200
            finally:
                release.set()
            assert first.result(timeout=3).status_code == 303


def test_recording_registry_write_failure_cannot_submit(tmp_path, monkeypatch):
    source = tmp_path / 'quiet.wav'
    silence(source)
    processor = ObservedProcessor()
    with TestClient(app_for(tmp_path / 'runtime', processor), raise_server_exceptions=False) as client:
        slug = _matter(client)
        bench, matter, document, token = upload(client, slug, source)
        job = wait_job(bench, matter, document)
        store = bench.source_store(matter)
        original = store._save
        writes = 0

        def fail_once(*args, **kwargs):
            nonlocal writes
            writes += 1
            if writes == 1:
                raise OSError('Synthetic registry write failure')
            return original(*args, **kwargs)

        monkeypatch.setattr(store, '_save', fail_once)
        response = client.post(f'/matters/{slug}/sources/{token}/recording-check', data={
            'action': 'continue', 'inspection_id': job.preflight['inspection_id']}, follow_redirects=False)
        finished = wait_job(bench, matter, document, ('failed', 'succeeded', 'degraded'))
        assert processor.submissions == 0
        assert response.status_code == 303
        assert finished.state == 'failed'


def test_audit_failure_leaves_recording_decision_unadmitted(tmp_path):
    source = tmp_path / 'quiet.wav'
    silence(source)
    processor = ObservedProcessor()
    with TestClient(app_for(tmp_path / 'runtime', processor), raise_server_exceptions=False) as client:
        slug = _matter(client)
        bench, matter, document, token = upload(client, slug, source)
        job = wait_job(bench, matter, document)
        bench.media.close()
        with fixture_write(bench.workspace) as fixture_connection:
            fixture_connection.execute("""CREATE TRIGGER synthetic_audit_failure
                BEFORE INSERT ON workbench_audit_event WHEN NEW.action LIKE 'media.preflight_%'
                BEGIN SELECT RAISE(ABORT, 'Synthetic audit failure'); END""")
        response = client.post(f'/matters/{slug}/sources/{token}/recording-check', data={
            'action': 'continue', 'inspection_id': job.preflight['inspection_id']}, follow_redirects=False)
        current = bench.workspace.media_job(matter.matter_id, document.document_id, document.version_id)
        assert current.state == 'cancelled'
        assert document.state == 'needs_review'
        assert response.status_code == 503
        assert processor.submissions == 0
        with fixture_write(bench.workspace) as fixture_connection:
            fixture_connection.execute('DROP TRIGGER synthetic_audit_failure')
        retried = client.post(f'/matters/{slug}/sources/{token}/recording-check', data={
            'action': 'continue', 'inspection_id': job.preflight['inspection_id']}, follow_redirects=False)
        assert retried.status_code == 303
        audits = [event for event in bench.workspace.audit_events(matter.matter_id)
                  if event.action == 'media.preflight_continue']
        assert len(audits) == 1


def test_recovered_legacy_submission_is_reconciled_before_speech_hold(tmp_path):
    from case_intelligence.media_evidence import processor_owner
    source = tmp_path / 'quiet.wav'
    silence(source, 8)
    processor = ObservedProcessor()
    runtime = tmp_path / 'runtime'
    with TestClient(app_for(runtime, processor)) as client:
        bench = client.app.state.workbench
        bench.media.close()
        slug = _matter(client)
        bench, matter, document, token = upload(client, slug, source)
        claimed = bench.workspace.claim_media_job('synthetic-legacy-worker')
        assert claimed is not None
        # Legacy crash window: processor accepted bytes, local ID was not saved.
        processor.submit(processor_owner(claimed.media_job_id), source, 'audio/wav', document.digest)
        assert claimed.external_job_id is None and not claimed.preflight
    with TestClient(app_for(runtime, processor)) as client:
        bench = client.app.state.workbench
        matter = bench.matter(slug, ACTOR)
        document = bench.source_store(matter).get_by_action_token(token)
        finished = wait_job(bench, matter, document, ('cancelled', 'failed', 'succeeded', 'degraded'))
        assert finished.state in {'succeeded', 'degraded'}
        assert processor.submissions == 1
        assert len(processor.deleted) == 1 and not processor.jobs


def test_recording_decision_budget_is_bounded_across_matters(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    source = tmp_path / 'quiet.wav'
    silence(source)
    release = threading.Event()
    two_entered = threading.Event()
    lock = threading.Lock()
    entered = 0
    with TestClient(app_for(tmp_path / 'runtime', ObservedProcessor())) as client:
        decisions = []
        for index in range(3):
            slug = _matter(client, f'Synthetic budget matter {index}')
            bench, matter, document, token = upload(client, slug, source)
            job = wait_job(bench, matter, document)
            store = bench.source_store(matter)
            original = store.source_path

            def blocked(*args, _original=original, **kwargs):
                nonlocal entered
                if kwargs.get('verify_digest'):
                    with lock:
                        entered += 1
                        if entered == 2:
                            two_entered.set()
                    assert release.wait(10)
                return _original(*args, **kwargs)

            monkeypatch.setattr(store, 'source_path', blocked)
            decisions.append((f'/matters/{slug}/sources/{token}/recording-check',
                              {'action': 'retry', 'inspection_id': job.preflight['inspection_id']}))
        with ThreadPoolExecutor(max_workers=2) as callers:
            futures = [callers.submit(client.post, url, data=data, follow_redirects=False)
                       for url, data in decisions[:2]]
            try:
                assert two_entered.wait(3)
                third = client.post(decisions[2][0], data=decisions[2][1], follow_redirects=False)
                assert third.status_code == 503 and third.headers['Retry-After'] == '3'
                assert entered == 2
            finally:
                release.set()
            assert all(future.result(timeout=3).status_code == 303 for future in futures)
        assert client.post(decisions[2][0], data=decisions[2][1], follow_redirects=False).status_code == 303
