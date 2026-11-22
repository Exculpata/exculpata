"""Synthetic proving tests for the Case Assistant synthesis evaluation runner."""
from __future__ import annotations

import json

from case_intelligence.case_assistant import AssistantFinding, AssistantInvestigationResult
from case_intelligence.case_assistant_eval import build_stock_eval_suite, main, score_eval_case
from case_intelligence.case_assistant_tools import SourceRef


def test_cli_scripted_stock_suite_scores_perfect_and_protects_overwrite(tmp_path):
    out_json = tmp_path / "eval.json"
    out_md = tmp_path / "eval.md"
    rc = main(["--backend", "scripted", "--seed", "7", "--out-json", str(out_json), "--out-md", str(out_md)])
    assert rc == 0
    report = json.loads(out_json.read_text(encoding="utf-8"))
    assert report["score"] == 100.0 and report["suite"] == "stock-ownership" and report["seed"] == 7
    assert len(report["cases"]) == 4
    assert all(row["score"] == 100.0 for row in report["cases"])
    md = out_md.read_text(encoding="utf-8")
    assert md.startswith("case-assistant-eval 100.0/100")
    assert "ownership_threshold" in md and "unrecorded_market_price_trap" in md

    # Overwrite protection refuses replacing an existing artifact without --force.
    assert main(["--backend", "scripted", "--seed", "7", "--out-json", str(out_json)]) == 2
    assert main(["--backend", "scripted", "--seed", "7", "--out-json", str(out_json), "--force"]) == 0


def test_scorer_zeros_false_answer_on_abstention_trap_and_penalizes_missing_receipt():
    suite, _ = build_stock_eval_suite(seed=7)
    trap = next(q for q in suite if q.case_id == "unrecorded_market_price_trap")
    ref = SourceRef("stock-000001", "SPND-000001-statement.pdf", "Page 1", "v1")
    false_answer = AssistantInvestigationResult(
        answerable=True, summary="The market price was $42.00.",
        findings=(AssistantFinding("The share price was 42.00.", (ref,), ()),),
        omitted_findings=0, gaps=(), competing_accounts=(),
        citations=(ref,), receipts={}, trace=(), turns_used=1, elapsed_ms=5,
    )
    trap_score = score_eval_case(trap, false_answer)
    assert trap_score["score"] == 0.0 and "trap_false_answer" in trap_score["penalties"]

    threshold = next(q for q in suite if q.case_id == "ownership_threshold")
    no_receipt = AssistantInvestigationResult(
        answerable=True, summary="Ownership exceeded 10%.",
        findings=(AssistantFinding("Combined ownership exceeded 10%.", (ref,), ()),),
        omitted_findings=1, gaps=(), competing_accounts=(),
        citations=(ref,), receipts={}, trace=(), turns_used=1, elapsed_ms=5,
    )
    partial = score_eval_case(threshold, no_receipt)
    assert partial["score"] < 50.0 and "missing_calculation_receipt" in partial["penalties"]
