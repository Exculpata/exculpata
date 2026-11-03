from datetime import date, datetime, timezone

import pytest
from pydantic import ValidationError

from case_intelligence.contracts import (
    CoverageReason,
    MeasuredRecall,
    ModelReadState,
    QueryCoverageReport,
    RecordTypeCoverage,
    coverage_limitations,
    elusion_recall,
)

NOW = datetime(2026, 10, 15, tzinfo=timezone.utc)


def _report(**changes: object) -> QueryCoverageReport:
    values: dict[str, object] = {
        "report_schema_version": "1",
        "query_id": "query-1",
        "coverage_as_of": NOW,
        "matter_scope": {"matter_id": "matter-1"},
        "search_lanes": ("lexical", "semantic"),
        "filters": {},
        "eligible_source_count": 24,
        "searched_source_count": 24,
        "model_read_full_count": 24,
        "model_read_partial_count": 0,
        "model_read_skipped_count": 0,
        "model_read_skipped_reasons": {},
        "failed_source_count": 0,
        "failed_source_reasons": {},
        "pending_source_count": 0,
        "changed_during_query_count": 0,
        "unknown_history_source_count": 0,
        "record_type_coverage": (),
        "measured_recall": None,
    }
    values.update(changes)
    return QueryCoverageReport.model_validate(values)


def test_contract_enums_use_design_values() -> None:
    assert {state.value for state in ModelReadState} == {
        "full",
        "partial",
        "skipped",
        "failed",
        "escalated",
        "not_requested",
    }
    assert CoverageReason.BUDGET_LIMIT == "budget_limit"
    assert CoverageReason.CANCELLED == "cancelled"


def test_zero_miss_recall_has_uncertainty() -> None:
    result = elusion_recall(150, 10_000, 150, 0)
    assert result.recall_estimate == 1.0
    assert result.recall_lower_bound < 1.0
    assert result.recall_upper_bound == 1.0


def test_recall_matches_hand_computed_clopper_pearson_bounds() -> None:
    # 4 / 400 elusion gives CP95 p=[0.002729, 0.025400]; mapping
    # found/(found + 10,000*p) gives recall=[0.7799, 0.9706].
    result = elusion_recall(900, 10_000, 400, 4)
    assert result.recall_estimate == pytest.approx(0.9)
    assert result.recall_lower_bound == pytest.approx(0.7799, abs=0.0001)
    assert result.recall_upper_bound == pytest.approx(0.9706, abs=0.0001)
    assert result.estimated_missed == pytest.approx(100)


@pytest.mark.parametrize(
    "args", [(-1, 1, 1, 0), (1, -1, 1, 0), (1, 1, -1, 0), (1, 1, 3, 4)]
)
def test_recall_rejects_invalid_counts(args: tuple[int, int, int, int]) -> None:
    with pytest.raises(ValueError):
        elusion_recall(*args)


def test_all_miss_sample_is_supported() -> None:
    result = elusion_recall(10, 100, 20, 20)
    assert result.recall_estimate == pytest.approx(10 / 110)
    assert result.recall_lower_bound == pytest.approx(10 / 110)
    assert result.recall_upper_bound > result.recall_lower_bound


def test_reason_groups_and_eligible_denominators_reconcile() -> None:
    with pytest.raises(ValidationError, match="skipped reasons"):
        _report(
            model_read_skipped_count=2, model_read_skipped_reasons={"budget_limit": 1}
        )
    with pytest.raises(ValidationError, match="eligible"):
        _report(searched_source_count=25)


def _record(
    record_type: str, eligible: int, attempted: int, produced: int, skipped: int = 0
) -> RecordTypeCoverage:
    return RecordTypeCoverage(
        record_type=record_type,
        schema_version="1",
        eligible_source_count=eligible,
        attempted_source_count=attempted,
        produced_source_count=produced,
        skipped_source_count=skipped,
        failed_source_count=0,
    )


def test_design_examples_round_trip_with_neutral_limitations() -> None:
    examples = [
        _report(record_type_coverage=(_record("dated-event", 24, 24, 3),)),
        _report(
            eligible_source_count=1_200,
            searched_source_count=1_180,
            model_read_full_count=960,
            model_read_partial_count=220,
            model_read_skipped_count=20,
            model_read_skipped_reasons={"source_not_ready": 20},
            pending_source_count=20,
            record_type_coverage=(_record("quoted-statement", 1_200, 1_180, 11, 20),),
            measured_recall=MeasuredRecall(
                method="random elusion sample",
                population_definition="unflagged quoted statements",
                sample_size=150,
                sample_misses=12,
                relevant_found=138,
                discard_set_size=1000,
                recall_estimate=0.92,
                recall_lower_bound=0.86,
                recall_upper_bound=0.96,
                estimated_missed=80,
                estimated_missed_upper=140,
                measured_on=date(2026, 10, 15),
            ),
        ),
        _report(
            eligible_source_count=87,
            searched_source_count=87,
            model_read_full_count=78,
            model_read_skipped_count=9,
            model_read_skipped_reasons={"unsupported_type": 9},
            failed_source_count=6,
            failed_source_reasons={"ocr:engine_failure": 6},
            record_type_coverage=(_record("timeline-entry", 87, 78, 2, 9),),
        ),
    ]
    forbidden = ("complete", "comprehensive", "all relevant", "cleared")
    for report in examples:
        restored = QueryCoverageReport.model_validate_json(report.model_dump_json())
        prose = " ".join(coverage_limitations(restored)).lower()
        assert not any(phrase in prose for phrase in forbidden)

    partial = coverage_limitations(examples[1])
    record_line = next(
        i for i, text in enumerate(partial) if "candidate for attorney review" in text
    )
    assert (
        next(i for i, text in enumerate(partial) if "partly supplied" in text)
        < record_line
    )
    assert next(i for i, text in enumerate(partial) if "pending" in text) < record_line
    assert partial[-1].startswith("Measured recall:")
