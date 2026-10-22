
from case_intelligence.legacy_names import (
    LEGACY_STEM,
)
from case_intelligence.model_portfolio_gold import (
    MODEL_PORTFOLIO_SCHEMA_VERSION,
    MODEL_PORTFOLIO_SUITE_ID,
    model_portfolio_cases,
    model_portfolio_fingerprint,
)


def test_model_portfolio_gold_is_frozen_and_structurally_valid() -> None:
    cases = model_portfolio_cases()
    assert MODEL_PORTFOLIO_SUITE_ID == f"{LEGACY_STEM}-model-portfolio-gold-v1"
    assert MODEL_PORTFOLIO_SCHEMA_VERSION == 1
    assert len(cases) == 10
    assert len({item.case_id for item in cases}) == len(cases)
    assert len({item.category for item in cases}) == len(cases)
    assert len(model_portfolio_fingerprint()) == 64
    for case in cases:
        identifiers = [item.evidence_id for item in case.evidence]
        assert 1 <= len(identifiers) <= 12
        assert identifiers == [f"S{index}" for index in range(1, len(identifiers) + 1)]
        assert all(len(item.excerpt) <= 6_000 for item in case.evidence)
        assert all(set(group).issubset(identifiers) for group in case.required_evidence_groups)


def test_model_portfolio_gold_contains_no_environment_coordinates() -> None:
    rendered = repr(model_portfolio_cases()).casefold()
    assert "/home/" not in rendered
    assert "\\users\\" not in rendered
    assert "http://" not in rendered
    assert "https://" not in rendered
    assert "@" not in rendered
