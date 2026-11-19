import pytest
from fastapi.testclient import TestClient

from case_intelligence.media_evidence import MediaProcessorError
from tests.fixture_write import fixture_write
from tests.test_matter_media_workflow import (
    ACTOR, WAV, ConfigurableCleanupMediaProcessor, OfflineMediaProcessor, _matter,
)
from tests.test_media_preflight import app_for, upload, wait_job


@pytest.mark.parametrize(("known_processor_job", "processor_online"), [
    (True, True), (False, True), (False, False),
])
def test_retry_recovers_saved_review_after_transcript_import(tmp_path, monkeypatch, known_processor_job, processor_online):
    class DegradedProcessor(ConfigurableCleanupMediaProcessor):
        def job(self, owner, external_job_id):
            return {**super().job(owner, external_job_id), "status": "degraded"}

    def interrupt_projection(*args):
        raise MediaProcessorError("Synthetic interruption after transcript import.")

    runtime = tmp_path / "runtime"
    processor = DegradedProcessor()
    processor.cleanup_outcome = "unavailable"
    with TestClient(app_for(runtime, processor)) as client:
        slug = _matter(client)
        with monkeypatch.context() as patch:
            patch.setattr(client.app.state.workbench.media, "project_transcript", interrupt_projection)
            bench, matter, document, token = upload(client, slug, WAV)
            failed = wait_job(bench, matter, document, ("failed",))
        assert failed.message == "Synthetic interruption after transcript import."
        assert failed.external_job_id
        keys = matter.matter_id, document.document_id, document.version_id
        transcript = bench.workspace.media_transcript(*keys)
        assert transcript is not None
        original = bench.workspace.transcript_segments(*keys)[0]
        source = f"/matters/{slug}/sources/{token}"
        correction = "The synthetic bicycle was logged at the south entrance."
        label = "Reviewed speaker"
        assert client.post(f"{source}/segments/{original.segment_id}", data={
            "expected_revision": 0, "text": correction,
        }, follow_redirects=False).status_code == 303
        assert client.post(f"{source}/speakers", data={
            "speaker_cluster": original.speaker_cluster, "expected_revision": 0,
            "display_name": label, "identity_state": "confirmed",
        }, follow_redirects=False).status_code == 303
        citation = bench.search(matter, "synthetic bicycle south entrance")[0]
        bench.media.close()
        assert client.post(source + "/retry", follow_redirects=False).status_code == 303
        if not known_processor_job:
            # Older releases cleared the processor ID when retry was requested.
            with fixture_write(bench.workspace) as connection:
                connection.execute("UPDATE workbench_media_job SET external_job_id=NULL WHERE media_job_id=?",
                                   (failed.media_job_id,))

    processor.cleanup_outcome = "success"
    with TestClient(app_for(runtime, processor if processor_online else OfflineMediaProcessor())) as client:
        bench = client.app.state.workbench
        matter = bench.matter(slug, ACTOR)
        document = bench.source_store(matter).get_by_action_token(token)
        finished = wait_job(bench, matter, document, ("failed", "degraded", "succeeded"))
        assert finished.state == "degraded", finished.message
        assert finished.source_sha256 == document.digest == failed.source_sha256
        assert finished.provenance == transcript.provenance
        assert finished.warnings == transcript.warnings
        assert all(finished.quality.get(key) == value for key, value in transcript.quality.items())
        assert bench.workspace.media_transcript(*keys).transcript_id == transcript.transcript_id
        reviewed = bench.workspace.transcript_segments(*keys)[0]
        assert (reviewed.segment_id, reviewed.model_text) == (original.segment_id, original.model_text)
        assert (reviewed.current_text, reviewed.speaker_display_name) == (correction, label)
        assert document.parsed_units()[0].text == f"{label}: {correction}"
        support = client.get(citation.href)
        assert support.status_code == 200 and 'data-start-ms="0"' in support.text
        assert correction in support.text
        if not processor_online:
            bench.media.close()
            refused = client.post(source + "/remove", follow_redirects=False)
            assert refused.status_code == 409 and "could not be removed yet" in refused.text
            assert not bench.media.cancel_matter(matter.matter_id)
    if not processor_online:
        assert processor.jobs
        with TestClient(app_for(runtime, processor)) as client:
            client.app.state.workbench.media.close()
            assert client.post(source + "/remove", follow_redirects=False).status_code == 303
    assert processor.submissions == 1
    assert processor.deleted == [(f"ci-{failed.media_job_id}", failed.external_job_id)]
    assert not processor.jobs
