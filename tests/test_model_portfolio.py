
from case_intelligence.legacy_names import (
    LEGACY_STEM,
)
from case_intelligence.model_portfolio import (
    APPROVED_GENERATOR_MODEL,
    DISCOVERY_GOLD_FINGERPRINT,
    PORTFOLIO_ID,
    model_portfolio_projection,
)
from case_intelligence import generation
import pytest
from tests.test_generator_thinking import backend, client, wire, _thinking  # noqa: F401


@pytest.mark.parametrize("disabled", ["0", "1"])
def test_environment_portfolio_direct_mode_matches_answer_request(monkeypatch, client, backend, wire, disabled):
    monkeypatch.setenv("CASE_INTELLIGENCE_GENERATOR_DISABLE_THINKING", disabled)
    monkeypatch.setenv("CASE_INTELLIGENCE_GENERATOR_MODEL", APPROVED_GENERATOR_MODEL)
    client = generation.generator_from_environment()
    monkeypatch.setattr(generation, "_bounded_json_get", lambda *_args, **_kwargs: {
        "models": [{"name": client.model}], "data": [{"id": client.model}],
    })
    client.generate(question="What is missing?", evidence=(), thinking=None)
    thinking = _thinking(backend, wire[-1])
    projection = _projection(generation.GroundedGenerationService(client))
    assert thinking is True
    assert projection["generator"]["ready"] is True
    assert projection["ready"] is True
    assert projection["generator"]["mode"] == "Direct structured generation"
    assert projection["lanes"][1]["tone"] == "ready"
    if backend == "openai":
        assert client.disable_thinking is (not thinking)


class _Client:
    def __init__(self, *, model: str = APPROVED_GENERATOR_MODEL) -> None:
        self.model = model
        self.endpoint = "http://127.0.0.1:8790"
        self.disable_thinking = True

    @property
    def available(self) -> bool:
        return True


class _Service:
    def __init__(self, *, model: str = APPROVED_GENERATOR_MODEL) -> None:
        self.client = _Client(model=model)
        self.maximum_active = 4

    @property
    def available(self) -> bool:
        return self.client.available


def _projection(service: _Service) -> dict[str, object]:
    return dict(
        model_portfolio_projection(
            service,  # type: ignore[arg-type]
            learned_retrieval=True,
            answer_workers=2,
            research_workers=1,
            review_workers=1,
            review_source_concurrency=2,
            media_ready=True,
        )
    )


def test_local_portfolio_projects_configured_task_lanes_without_quality_claims() -> None:
    projection = _projection(_Service())

    assert projection["portfolio_id"] == PORTFOLIO_ID
    assert projection["ready"] is True
    assert projection["evaluation"]["suite_fingerprint"] == DISCOVERY_GOLD_FINGERPRINT
    assert projection["evaluation"]["quality_metrics_published"] is False
    assert "gold" not in projection
    assert [lane["name"] for lane in projection["lanes"]] == [
        "Find & order",
        "Focused answer",
        "Broader investigation",
        "Every-source check",
    ]
    assert projection["generator"]["maximum_active"] == 4
    assert projection["evidence"]["maximum_items"] == 12


def test_unreviewed_generator_is_visible_but_not_reported_ready() -> None:
    projection = _projection(_Service(model="unreviewed-candidate"))

    assert projection["ready"] is False
    assert projection["generator"]["ready"] is False
    assert projection["generator"]["configured_model"] == "unreviewed-candidate"


def test_previous_portable_served_name_stays_a_reviewed_configuration() -> None:
    projection = _projection(_Service(model=f"{LEGACY_STEM}-qwen35-4b"))

    assert projection["ready"] is True
    assert projection["generator"]["profile"] == "portable"
    assert projection["generator"]["repository"] == "Qwen/Qwen3.5-4B"


def test_quality_generator_tier_is_also_a_reviewed_configuration() -> None:
    projection = _projection(_Service(model=f"{LEGACY_STEM}-qwen35-9b"))

    assert projection["ready"] is True
    assert projection["generator"]["profile"] == "quality"
    assert projection["generator"]["repository"] == "Qwen/Qwen3.5-9B"
