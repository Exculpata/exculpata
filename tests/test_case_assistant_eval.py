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


def test_pr591_regressions_trap_rejections_unused_receipts_contradiction_and_atomic_write(tmp_path, monkeypatch):
    from case_intelligence import case_assistant_eval as eval_mod
    from case_intelligence.case_assistant_tools import CalculationReceipt

    suite, _ = build_stock_eval_suite(seed=7)
    trap = next(q for q in suite if q.case_id == "unrecorded_market_price_trap")
    threshold = next(q for q in suite if q.case_id == "ownership_threshold")
    contradiction = next(q for q in suite if q.case_id == "associate_contradiction")
    filing_ref = SourceRef("stock-1", "SPND-000001-filing.txt", "Page 1", "v1")
    stmt_ref = SourceRef("stock-2", "SPND-000002-statement.txt", "Page 1", "v1")
    iv_ref = SourceRef("stock-3", "SPND-000003-interview.txt", "Page 1", "v1")
    em_ref = SourceRef("stock-4", "SPND-000004-email.txt", "Page 1", "v1")

    # 1. Rejected trap guess (omitted_findings=1) and unstructured output must score 0, not 100.
    rejected_trap = AssistantInvestigationResult(
        answerable=False, summary="The inspected sources did not support a verified finding.",
        findings=(), omitted_findings=1, gaps=(), competing_accounts=(),
        citations=(), receipts={}, trace=(), turns_used=1, elapsed_ms=5,
    )
    assert score_eval_case(trap, rejected_trap)["score"] == 0.0
    unstructured_trap = AssistantInvestigationResult(
        answerable=False, summary="Parse error.", findings=(), omitted_findings=0,
        gaps=("Model returned unstructured output.",), competing_accounts=(),
        citations=(), receipts={}, trace=(), turns_used=1, elapsed_ms=5,
    )
    assert score_eval_case(trap, unstructured_trap)["score"] == 0.0

    # 2. Unused receipt with the right number cannot rescue a wrong final answer.
    unused_receipt = CalculationReceipt("calc-2", "percent", "expr", str(threshold.expected_value), "%", ())
    wrong_answer = AssistantInvestigationResult(
        answerable=True, summary="Controlled accounts never exceeded the threshold.",
        findings=(AssistantFinding("Controlled accounts never exceeded the threshold.", (filing_ref, stmt_ref), ()),),
        omitted_findings=0, gaps=("1 unavailable: damaged-scan.pdf",), competing_accounts=(),
        citations=(filing_ref, stmt_ref), receipts={"calc-2": unused_receipt}, trace=(), turns_used=2, elapsed_ms=5,
    )
    wrong_score = score_eval_case(threshold, wrong_answer)
    assert "numeric_mismatch" in wrong_score["penalties"]
    assert "missing_calculation_receipt" in wrong_score["penalties"]
    assert wrong_score["score"] < 50.0

    # 3. Saying interviews are consistent with emails fails contradiction accuracy.
    opposite_contradiction = AssistantInvestigationResult(
        answerable=True, summary="The interviews are consistent with the emails.",
        findings=(AssistantFinding("The interviews are consistent with the emails.", (iv_ref, em_ref), ()),),
        omitted_findings=0, gaps=(), competing_accounts=("Mentioned competing account.",),
        citations=(iv_ref, em_ref), receipts={}, trace=(), turns_used=2, elapsed_ms=5,
    )
    opp_score = score_eval_case(contradiction, opposite_contradiction)
    assert "conclusion_mismatch" in opp_score["penalties"] and opp_score["score"] <= 60.0

    # 4. Concurrent file creation during run_evaluation is refused at open('x') without overwriting.
    raced = tmp_path / "raced.json"
    real_run = eval_mod.run_evaluation

    def _raced_run(*args, **kwargs):
        raced.write_text('{"winner": "first"}\n', encoding="utf-8")
        return real_run(*args, **kwargs)

    monkeypatch.setattr(eval_mod, "run_evaluation", _raced_run)
    rc = main(["--backend", "scripted", "--seed", "7", "--out-json", str(raced)])
    assert rc == 2 and json.loads(raced.read_text(encoding="utf-8")) == {"winner": "first"}

