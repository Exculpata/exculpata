
from case_intelligence.legacy_names import (
    LEGACY_STEM,
)
from case_intelligence.model_portfolio import (
    APPROVED_GENERATOR_MODEL,
    DISCOVERY_GOLD_FINGERPRINT,
    PORTFOLIO_ID,
    model_portfolio_projection,
)


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
