"""Synthetic HTTP edits, durable stores and restart reconciliation; no models."""
import hashlib
import io
import shutil
import wave
from urllib.parse import unquote_plus

import pytest
from fastapi.testclient import TestClient

from case_intelligence import workbench as module
from case_intelligence.generation import UnavailableGenerator
from case_intelligence.pilot_uploads import PilotDocument

ACTOR = "development-taylor-morgan"


class SearchProjection:
    """Only the external PostgreSQL transport is replaced in this composition."""
    def __init__(self):
        self.documents = {}
        self.fail = False
        self.writes = 0
        self.rollbacks = 0

    def cursor(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def execute(self, sql, params):
        assert sql.startswith("SELECT document_id,current_source_version_id")
        self.matter_id = params[0]

    def fetchall(self):
        return [(key[1], value["source_version_id"]) for key, value in self.documents.items()
                if key[0] == self.matter_id]

    def commit(self):
        pass

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        pass

    def upsert(self, _connection, **kwargs):
        if self.fail:
            raise RuntimeError("Synthetic search service outage")
        self.documents[(kwargs["matter_id"], kwargs["document_id"])] = kwargs
        self.writes += 1


@pytest.fixture
def projection(monkeypatch):
    monkeypatch.setenv("CASE_INTELLIGENCE_STORAGE_RESERVE_GIB", "0")
    result = SearchProjection()
    monkeypatch.setattr(module, "apply_postgres_migrations", lambda _: None)
    monkeypatch.setattr(module, "retain_postgres_workbench_matters", lambda *a, **kw: None)
    monkeypatch.setattr(module, "upsert_postgres_document", result.upsert)
    return result


def app(runtime, projection):
    return module.create_workbench_app(runtime, auth_mode="test", generator=UnavailableGenerator(),
        postgres_connection=projection, learned_retrieval=False)


def seed(client, *, with_transcript=True):
    bench = client.app.state.workbench
    bench.media.close()
    response = client.post("/matters", data={"name": "Synthetic search freshness"}, follow_redirects=False)
    matter = bench.matter(response.headers["location"].split("/")[2], ACTOR)
    store = bench.source_store(matter)
    stream = io.BytesIO()
    with wave.open(stream, "wb") as audio:
        audio.setparams((1, 2, 8000, 0, "NONE", "not compressed"))
        audio.writeframes(b"\x00\x00" * 8000)
    original = stream.getvalue()
    document = PilotDocument("a" * 32, "Synthetic transcript.wav", "a" * 32 + ".wav",
        "audio/wav", len(original), "ready", "", [],
        digest=hashlib.sha256(original).hexdigest(), version_id="b" * 32, duration_ms=1000)
    (store.files / document.stored_name).write_bytes(original)
    document._units_loader, document._units_iterator = store._load_units, store._iter_units
    with store.mutation_guard():
        store.documents[document.document_id] = document
        store._save((document.document_id,))
    job = bench.workspace.queue_media_job(matter.matter_id, document.document_id, document.version_id, ACTOR,
        source_sha256=document.digest, byte_size=document.size, media_type=document.media_type, duration_ms=1000)
    if not with_transcript:
        with store.mutation_guard():
            document.state = "queued"
            store._save((document.document_id,))
        return bench, matter, document, f"/matters/{matter.slug}/sources/{store.action_token(document)}"
    assert bench.workspace.claim_media_job("synthetic-freshness-worker").media_job_id == job.media_job_id
    bench.workspace.import_media_transcript(job.media_job_id, segments=[{
        "external_segment_id": "synthetic-segment", "start_ms": 0, "end_ms": 1000,
        "speaker_cluster": "SPEAKER_00", "model_text": "Synthetic blue bicycle.",
    }], warnings=[], quality={}, provenance={})
    bench.workspace.finish_media_job(job.media_job_id, degraded=False, message="")
    bench.refresh_media_projection(matter, document)
    return bench, matter, document, f"/matters/{matter.slug}/sources/{store.action_token(document)}"


@pytest.mark.parametrize("edit", ["text", "speaker"])
def test_saved_edit_reports_pending_and_restart_repairs_same_source_version(tmp_path, monkeypatch, projection, edit):
    runtime = tmp_path / "runtime"
    with TestClient(app(runtime, projection), raise_server_exceptions=False) as client:
        bench, matter, document, url = seed(client)
        keys = (matter.matter_id, document.document_id, document.version_id)
        segment = bench.workspace.transcript_segments(*keys)[0]
        old_candidate = bench._candidate(matter, document, document.parsed_units()[0], 1)
        projection.fail = True
        path = f"{url}/segments/{segment.segment_id}" if edit == "text" else f"{url}/speakers"
        data = {"expected_revision": 0, "text": "Synthetic orange unicycle."} if edit == "text" else {
            "expected_revision": 0, "speaker_cluster": "SPEAKER_00", "display_name": "Synthetic speaker", "identity_state": "confirmed"}
        response = client.post(path, data=data, follow_redirects=False)
        assert response.status_code == 303
        assert "saved" in unquote_plus(response.headers["location"])
        assert "search update pending" in unquote_plus(response.headers["location"])
        assert not bench._validate_candidates(matter, (old_candidate,))
        assert document.version_id == keys[2]
        fresh_page = client.get(url)
        assert "Search update pending" in fresh_page.text
        assert "Retry search update" in fresh_page.text
    with TestClient(app(runtime, projection)) as client:
        assert "Search update pending" in client.get(url).text
        assert client.app.state.workbench.workspace.transcript_segments(*keys)[0].current_text == (
            "Synthetic orange unicycle." if edit == "text" else "Synthetic blue bicycle.")
    # A stopped-state backup/clean restore retains both authoritative revisions
    # and the unacknowledged projection receipt, including registry/WAL state.
    backup, restored = tmp_path / "backup", tmp_path / "restored"
    shutil.copytree(runtime, backup)
    shutil.copytree(backup, restored)
    projection.fail = False
    with TestClient(app(restored, projection)) as client:
        bench = client.app.state.workbench
        current = bench.source_store(matter).get(document.document_id)
        indexed = projection.documents[keys[:2]]
        expected = "orange unicycle" if edit == "text" else "Synthetic speaker"
        assert expected in indexed["units"][0].text
        assert indexed["source_version_id"] == keys[2] == current.version_id
        assert indexed["content_digest"] == document.digest
        native = bench.source_store(matter).source_path(current.document_id, verify_digest=True).read_bytes()
        assert hashlib.sha256(native).hexdigest() == document.digest
        assert bench._validate_candidates(matter, (bench._candidate(matter, current, current.parsed_units()[0], 1),))
        assert "Search update pending" not in client.get(url).text
        writes = projection.writes
        bench._rebuild_all_indexes()
        assert projection.writes == writes


@pytest.mark.parametrize("failure", ["local", "offline", "receipt", "index"])
def test_projection_failure_remains_visible_and_retry_is_not_another_revision(tmp_path, monkeypatch, projection, failure):
    runtime = tmp_path / "runtime"
    with TestClient(app(runtime, projection), raise_server_exceptions=False) as client:
        bench, matter, document, url = seed(client)
        store = bench.source_store(matter)
        keys = (matter.matter_id, document.document_id, document.version_id)
        segment = bench.workspace.transcript_segments(*keys)[0]
        old_candidate = bench._candidate(matter, document, document.parsed_units()[0], 1)
        with monkeypatch.context() as patch:
            if failure == "local":
                def unavailable(*a, **kw):
                    raise OSError("Synthetic projection unavailable")
                patch.setattr(store, "install_media_transcript", unavailable)
            elif failure == "offline":
                patch.setattr(bench, "postgres_ready", False)
            elif failure == "receipt":
                original = store._save
                def fail_receipt(*a, **kw):
                    if document.transcript_index_basis != old_basis:
                        raise OSError("Synthetic receipt failure")
                    return original(*a, **kw)
                old_basis = document.transcript_index_basis
                patch.setattr(store, "_save", fail_receipt)
            else:
                patch.setattr(projection, "fail", True)
            response = client.post(f"{url}/segments/{segment.segment_id}",
                data={"expected_revision": 0, "text": "Synthetic orange unicycle."}, follow_redirects=False)
            assert response.status_code == 303
            assert "pending" in response.headers["location"]
            assert not bench._validate_candidates(matter, (old_candidate,))
            assert "Search update pending" in client.get(url).text
        response = client.post(f"{url}/search-refresh", follow_redirects=False)
        assert response.status_code == 303
        assert "Transcript+search+refreshed" in response.headers["location"]
        assert bench.workspace.transcript_segments(*keys)[0].current_revision == 1
        assert "Search update pending" not in client.get(url).text
        assert "orange unicycle" in projection.documents[keys[:2]]["units"][0].text


def test_crash_after_edit_before_projection_is_repaired_from_durable_revision(tmp_path, projection):
    runtime = tmp_path / "runtime"
    with TestClient(app(runtime, projection)) as client:
        bench, matter, document, url = seed(client)
        keys = (matter.matter_id, document.document_id, document.version_id)
        segment = bench.workspace.transcript_segments(*keys)[0]
        bench.workspace.revise_transcript_segment(*keys, segment.segment_id,
            expected_revision=0, text="Synthetic orange unicycle.", actor_id=ACTOR)
        # Simulate process loss before the HTTP handler begins projection.
    with TestClient(app(runtime, projection)) as client:
        assert "orange unicycle" in projection.documents[keys[:2]]["units"][0].text
        assert "Search update pending" not in client.get(url).text


def test_old_registry_without_receipt_is_backfilled_once(tmp_path, projection):
    import json
    runtime = tmp_path / "runtime"
    with TestClient(app(runtime, projection)) as client:
        bench, matter, document, _ = seed(client)
        store = bench.source_store(matter)
        payload = store._document_payload(document)
        payload.pop("transcript_index_basis", None)
        with store.mutation_guard(), store._registry:
            store._registry.execute("UPDATE source_document SET payload=? WHERE document_id=?",
                (json.dumps(payload), document.document_id))
    before = projection.writes
    with TestClient(app(runtime, projection)) as client:
        bench = client.app.state.workbench
        assert projection.writes == before + 1
        bench._rebuild_all_indexes()
        assert projection.writes == before + 1


def test_speaker_json_reports_saved_pending_and_retry_enforces_membership(tmp_path, projection):
    with TestClient(app(tmp_path / "runtime", projection)) as client:
        bench, matter, document, url = seed(client)
        projection.fail = True
        response = client.post(f"{url}/speakers", data={"expected_revision": 0,
            "speaker_cluster": "SPEAKER_00", "display_name": "Synthetic speaker", "identity_state": "confirmed"},
            headers={"Accept": "application/json"})
        assert response.status_code == 200
        assert response.json()["search_refreshed"] is False
        assert "saved; search update pending" in response.json()["message"]
        other = bench.workspace.create_matter("Synthetic other matter", "", ACTOR)
        assert client.post(f"{url.replace(matter.slug, other.slug)}/search-refresh").status_code == 404


def test_retry_requires_csrf_and_membership(tmp_path, projection):
    from tests.test_identity_membership_audit import _login
    runtime = tmp_path / "runtime"
    with TestClient(app(runtime, projection)) as client:
        _, matter, _, url = seed(client)
    preview = module.create_workbench_app(runtime, auth_mode="preview", generator=UnavailableGenerator(),
        postgres_connection=projection, learned_retrieval=False)
    with TestClient(preview) as client:
        _, csrf = _login(client, "taylor-morgan")
        for token in (None, "invalid"):
            fields = {} if token is None else {"csrf_token": token}
            assert client.post(f"{url}/search-refresh", data=fields).status_code == 403
        assert client.post(f"{url}/search-refresh", data={"csrf_token": csrf}, follow_redirects=False).status_code == 303
        client.cookies.clear()
        _, csrf = _login(client, "alex-rivera")
        assert client.post(f"{url}/search-refresh", data={"csrf_token": csrf}, follow_redirects=False).status_code in (403, 404)


def test_retry_without_transcript_does_not_fail_a_queued_recording(tmp_path, projection):
    with TestClient(app(tmp_path / "runtime", projection)) as client:
        bench, matter, document, url = seed(client, with_transcript=False)
        keys = (matter.matter_id, document.document_id, document.version_id)
        before = bench.workspace.media_job(*keys)
        response = client.post(f"{url}/search-refresh", follow_redirects=False)
        assert response.status_code == 404
        assert document.state == "queued"
        assert document.processing_stage != "Search update pending"
        assert bench.workspace.media_job(*keys) == before
        assert not bench.workspace.transcript_segments(*keys)
