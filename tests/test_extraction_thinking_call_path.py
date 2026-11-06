"""Production full-text review keeps extraction independent of answer thinking."""

import io

import pytest
from fastapi.testclient import TestClient

from case_intelligence import generation
from case_intelligence.workbench import create_workbench_app
from tests.test_generator_thinking import backend, client, wire, _thinking  # noqa: F401


@pytest.mark.parametrize("disabled", ["0", "1"])
def test_full_text_review_sends_non_thinking_extraction(monkeypatch, tmp_path, client, backend, wire, disabled):
    monkeypatch.setenv("CASE_INTELLIGENCE_GENERATOR_DISABLE_THINKING", disabled)
    client = generation.generator_from_environment()
    monkeypatch.setattr(generation, "_bounded_json_get", lambda *_args, **_kwargs: {
        "models": [{"name": client.model}], "data": [{"id": client.model}],
    })
    app = create_workbench_app(tmp_path / "runtime", generator=client, auth_mode="test")
    with TestClient(app):
        bench = app.state.workbench
        bench.full_review.close()
        bench.research.close()
        actor = "development-taylor-morgan"
        matter = bench.create_matter("Synthetic extraction", "", actor)
        store = bench.source_store(matter)
        document, _ = store.store_stream(
            "synthetic.txt", "text/plain", io.BytesIO(b"Synthetic passage without a match."),
        )
        bench._sync_source_catalog(matter, [document])
        _, version = bench.workspace.create_review_criterion(
            matter.matter_id, actor, title="Reference", instructions="Find an express reference.",
        )
        bench.workspace.queue_review_run(matter.matter_id, actor, version.criterion_version_id,
                                        run_kind="full", review_mode="full_text")
        run = bench.workspace.claim_review_run("synthetic-extraction-worker")
        decision = bench.workspace.next_review_decision(run.run_id)
        outcome = bench._process_review_decision(run, decision, lambda: False)
        assert outcome.decision == "excluded"
        assert len(wire) == 1
        assert _thinking(backend, wire[0]) is False
        assert (wire[0]["options"]["num_predict"] if backend == "ollama" else wire[0]["max_tokens"]) == 400
