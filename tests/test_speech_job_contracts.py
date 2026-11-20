"""Durable review and matter authorization across the four operator speech services."""

from collections import Counter
from contextlib import contextmanager
from http.server import ThreadingHTTPServer
import socket
import ssl
import subprocess
import threading

import pytest
from fastapi.testclient import TestClient

from case_intelligence.media_evidence import MediaProcessorError
from tests.speech_contract_provider import SpeechContractHandler
from tests.test_egress_proxy import proxy
from tests.test_external_model_grant_routes import _grant_forms
from tests.test_matter_media_workflow import ACTOR, OfflineMediaProcessor, _matter
from tests.test_media_preflight import SPEECH, app_for, upload, wait_job


ROLES = {
    "transcription": ("ASR", "/v1/audio/transcriptions"),
    "alignment": ("ALIGNMENT", "/v1/audio/alignments"),
    "translation": ("TRANSLATION", "/v1/translations"),
    "speaker_labeling": ("DIARIZATION", "/v1/audio/speaker-labels"),
}


@contextmanager
def serving(server):
    with server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield
        finally:
            server.shutdown()
            thread.join()


@pytest.fixture
def speech_services(tmp_path, monkeypatch):
    monkeypatch.setenv("CASE_INTELLIGENCE_STORAGE_RESERVE_GIB", "0")
    paths = []
    asr_received = threading.Event()
    release_asr = threading.Event()
    release_asr.set()

    class ObservedProvider(SpeechContractHandler):
        def do_POST(self):
            paths.append(self.path)
            if self.path == ROLES["transcription"][1]:
                asr_received.set()
                if not release_asr.wait(10):
                    self.send_error(503)
                    return
            super().do_POST()

    key, certificate = tmp_path / "speech.key", tmp_path / "speech.crt"
    subprocess.run([
        "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
        "-keyout", str(key), "-out", str(certificate), "-subj", "/CN=*.example.test",
        "-addext", "subjectAltName=DNS:*.example.test",
    ], capture_output=True, check=True, timeout=10)
    monkeypatch.setenv("SSL_CERT_FILE", str(certificate))
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certificate, key)
    provider = ThreadingHTTPServer(("127.0.0.1", 0), ObservedProvider)
    provider.socket = context.wrap_socket(provider.socket, server_side=True)
    settings = {}
    for role, _ in ROLES.values():
        host = f"{role.lower()}.example.test"
        origin = "https://" + host
        settings[role + "_EGRESS_URL"] = origin
        prefix = f"CASE_INTELLIGENCE_{role}_"
        for name, value in {"URL": origin, "MODEL": "synthetic-" + role.lower(),
                            "ALLOWED_HOSTS": host, "REMOTE_CONFIRMED": "1"}.items():
            monkeypatch.setenv(prefix + name, value)
    allowed = proxy.configured_destinations(settings)
    relay = proxy.Server(("127.0.0.1", 0), allowed)

    def dial_local_fixture(host, port, pins):
        assert (host, port) in allowed
        return socket.create_connection(provider.server_address, timeout=5)

    monkeypatch.setattr(proxy, "dial", dial_local_fixture)
    for role, _ in ROLES.values():
        monkeypatch.setenv(f"CASE_INTELLIGENCE_{role}_PROXY", f"http://127.0.0.1:{relay.server_address[1]}")
    with serving(provider), serving(relay):
        try:
            yield paths, asr_received, release_asr
        finally:
            release_asr.set()


def set_grants(client, slug, capabilities, decision="approve"):
    path = f"/matters/{slug}/model-grants"
    page = client.get(f"/matters/{slug}/setup")
    assert page.status_code == 200
    forms = _grant_forms(page, path)
    for capability in capabilities:
        form = forms[capability]
        assert form["decision"] == decision
        response = client.post(path, data=form, follow_redirects=False)
        assert response.status_code == 303, response.text


def test_enriched_recording_keeps_reviewed_citation_on_offline_retry(tmp_path, monkeypatch, speech_services):
    paths, _, _ = speech_services
    runtime = tmp_path / "runtime"

    def interrupt_after_import(*args):
        raise MediaProcessorError("Synthetic interruption after transcript import.")

    with TestClient(app_for(runtime, OfflineMediaProcessor())) as client:
        slug = _matter(client)
        set_grants(client, slug, ROLES)
        bench = client.app.state.workbench
        with monkeypatch.context() as interruption:
            interruption.setattr(bench.media, "project_transcript", interrupt_after_import)
            _, matter, document, token = upload(client, slug, SPEECH)
            failed = wait_job(bench, matter, document, ("failed",))
        assert failed.message == "Synthetic interruption after transcript import."
        assert Counter(paths) == Counter(path for _, path in ROLES.values())
        keys = matter.matter_id, document.document_id, document.version_id
        [segment] = bench.workspace.transcript_segments(*keys)
        assert segment.model_text == "Grabación de prueba."
        assert segment.translated_text == "Test recording."
        assert segment.speaker_cluster != "UNKNOWN"
        assert (segment.start_ms, segment.end_ms) == (110, 5390)
        source = f"/matters/{slug}/sources/{token}"
        reviewed_text = "Synthetic marker at the south entrance."
        name = "Reviewed speaker"
        assert client.post(f"{source}/segments/{segment.segment_id}", data={
            "expected_revision": 0, "text": reviewed_text,
        }, follow_redirects=False).status_code == 303
        assert client.post(source + "/speakers", data={
            "speaker_cluster": segment.speaker_cluster, "expected_revision": 0,
            "display_name": name, "identity_state": "confirmed",
        }, follow_redirects=False).status_code == 303
        citation = bench.search(matter, "synthetic marker south entrance")[0]
        bench.media.close()
        assert client.post(source + "/retry", follow_redirects=False).status_code == 303

    for role, _ in ROLES.values():
        monkeypatch.delenv(f"CASE_INTELLIGENCE_{role}_URL")
    with TestClient(app_for(runtime, OfflineMediaProcessor())) as client:
        bench = client.app.state.workbench
        matter = bench.matter(slug, ACTOR)
        reloaded_document = bench.source_store(matter).get_by_action_token(token)
        job = wait_job(bench, matter, reloaded_document, ("succeeded", "degraded", "failed"))
        assert job.state == "succeeded", job.message
        assert job.source_sha256 == reloaded_document.digest == failed.source_sha256
        [reviewed] = bench.workspace.transcript_segments(*keys)
        assert (reviewed.segment_id, reviewed.start_ms, reviewed.end_ms) == (
            segment.segment_id, segment.start_ms, segment.end_ms,
        )
        assert (reviewed.current_text, reviewed.speaker_display_name) == (reviewed_text, name)
        assert (reviewed.model_text, reviewed.translated_text) == (segment.model_text, segment.translated_text)
        support = client.get(citation.href)
        assert support.status_code == 200 and reviewed_text in support.text
        assert f'data-start-ms="{segment.start_ms}"' in support.text
        assert Counter(paths) == Counter(path for _, path in ROLES.values())


def test_worker_rechecks_matter_grant_between_transcription_and_speaker_dispatch(tmp_path, speech_services):
    paths, asr_received, release_asr = speech_services
    with TestClient(app_for(tmp_path / "runtime", OfflineMediaProcessor())) as client:
        approved = _matter(client, "Synthetic approved recording")
        separate = _matter(client, "Synthetic separate recording")
        set_grants(client, approved, ROLES)
        bench, matter, document, _ = upload(client, separate, SPEECH)
        denied = wait_job(bench, matter, document, ("failed",))
        assert denied.source_sha256 == document.digest
        assert not paths
        assert bench.workspace.media_transcript(matter.matter_id, document.document_id, document.version_id) is None

        release_asr.clear()
        _, matter, document, _ = upload(client, approved, SPEECH)
        try:
            assert asr_received.wait(10)
            set_grants(client, approved, ("speaker_labeling",), decision="revoke")
        finally:
            release_asr.set()
        complete = wait_job(bench, matter, document, ("succeeded", "degraded", "failed"))
        assert complete.state == "degraded", complete.message
        assert Counter(paths) == Counter(path for capability, (_, path) in ROLES.items()
                                        if capability != "speaker_labeling")
        [segment] = bench.workspace.transcript_segments(matter.matter_id, document.document_id, document.version_id)
        assert segment.model_text == "Grabación de prueba." and segment.translated_text == "Test recording."
        assert segment.speaker_cluster == "UNKNOWN"
        assert complete.quality["speaker_labeling_status"] == "denied"
