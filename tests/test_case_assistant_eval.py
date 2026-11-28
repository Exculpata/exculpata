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


def test_pr591_round2_pagination_exact_mode_and_report_metadata(tmp_path):
    import pytest
    from case_intelligence.case_assistant_eval import render_markdown
    from case_intelligence.case_assistant_tools import AssistantToolError, MatterToolRuntime

    _, backend = build_stock_eval_suite(seed=7)
    runtime = MatterToolRuntime(backend.matter_id, "eval-defender", backend)

    # 1. Pagination metadata reflects the full match count and unavailable docs.
    page1 = runtime.invoke("search_discovery", {"query": "shares", "limit": 5})
    assert len(page1["hits"]) == 5
    assert page1["total_matches"] > 100
    assert page1["has_more"] is True
    assert page1["next_offset"] == 5
    assert page1["excluded_unavailable_count"] == 1
    page2 = runtime.invoke("search_discovery", {"query": "shares", "limit": 5, "offset": 5})
    assert len(page2["hits"]) == 5
    assert page2["hits"] != page1["hits"]

    # 2. Exact mode enforces phrase and Boolean semantics rather than OR-of-tokens.
    exact = runtime.invoke(
        "search_discovery",
        {"query": '"never received trading instructions"', "mode": "exact", "limit": 20},
    )
    assert exact["total_matches"] >= 1
    assert all("never received trading instructions" in h["excerpt"].casefold() for h in exact["hits"])
    assert not any("trading schedule" in h["excerpt"].casefold() for h in exact["hits"])
    with pytest.raises(AssistantToolError, match="Invalid exact query"):
        runtime.invoke("search_discovery", {"query": '"unclosed phrase', "mode": "exact"})

    # 3. Saved JSON and Markdown artifacts identify backend, model, max_turns, and synthetic reference runs.
    out_json = tmp_path / "meta.json"
    out_md = tmp_path / "meta.md"
    assert main([
        "--backend", "scripted", "--seed", "7", "--max-turns", "9",
        "--out-json", str(out_json), "--out-md", str(out_md),
    ]) == 0
    report = json.loads(out_json.read_text(encoding="utf-8"))
    assert report["backend"] == "scripted"
    assert report["model"] == "scripted-reference"
    assert report["max_turns"] == 9
    assert report["synthetic_reference"] is True
    md = out_md.read_text(encoding="utf-8")
    assert "backend=scripted" in md and "model=scripted-reference" in md and "max_turns=9" in md
    assert "Synthetic reference run" in md
    live_md = render_markdown({**report, "backend": "ollama", "model": "qwen3:32b", "synthetic_reference": False})
    assert "backend=ollama" in live_md and "model=qwen3:32b" in live_md
    assert "Synthetic reference run" not in live_md


def test_extended_eval_suite_covers_bates_email_transcript_and_table_tools(tmp_path):
    import pytest
    from case_intelligence.case_assistant_eval import build_extended_eval_suite
    from case_intelligence.case_assistant_tools import AssistantAccessError, MatterToolRuntime

    out_json = tmp_path / "extended.json"
    out_md = tmp_path / "extended.md"
    rc = main(["--backend", "scripted", "--suite", "extended", "--seed", "7", "--out-json", str(out_json), "--out-md", str(out_md)])
    assert rc == 0
    report = json.loads(out_json.read_text(encoding="utf-8"))
    assert report["suite"] == "extended" and report["score"] == 100.0
    case_ids = {row["case_id"] for row in report["cases"]}
    expected_new = {
        "bates_gap_production_audit",
        "email_thread_attachment_gap",
        "transcript_window_contradiction",
        "table_ledger_aggregation",
    }
    assert expected_new.issubset(case_ids)
    assert len(report["cases"]) == 8
    assert all(row["score"] == 100.0 for row in report["cases"])
    md = out_md.read_text(encoding="utf-8")
    assert "suite=extended" in md and "bates_gap_production_audit" in md and "table_ledger_aggregation" in md

    # Scorer penalizes missing attachment-gap disclosure on email_thread_attachment_gap.
    suite, backend = build_extended_eval_suite(seed=7)
    email_case = next(c for c in suite if c.case_id == "email_thread_attachment_gap")
    email_ref = SourceRef("ext-email-2", "EXT-0002-email.eml", "Page 1", "v1-ext-email-2")
    no_gap_res = AssistantInvestigationResult(
        answerable=True, summary="The thread discussed a side letter.",
        findings=(AssistantFinding("Message 2 referenced a side letter attachment.", (email_ref,), ()),),
        omitted_findings=0, gaps=(), competing_accounts=(),
        citations=(email_ref,), receipts={}, trace=(), turns_used=2, elapsed_ms=5,
    )
    scored_no_gap = score_eval_case(email_case, no_gap_res)
    assert "missed_unreadable_gap" in scored_no_gap["penalties"] and scored_no_gap["score"] < 100.0

    # Backend enforces document-scope isolation on rich tools (including suppressing load_file_attempts).
    from case_intelligence.case_assistant_tools import AssistantToolError

    scoped = MatterToolRuntime(backend.matter_id, "eval-defender", backend, allowed_document_ids=frozenset({"ext-ledger"}))
    with pytest.raises(AssistantAccessError):
        scoped.invoke("read_email_thread", {"document_id": "ext-email-1"})
    with pytest.raises(AssistantAccessError):
        scoped.invoke("read_transcript_window", {"document_id": "ext-transcript", "start_seconds": 0, "end_seconds": 120})
    scoped_prod = scoped.invoke("inspect_production", {})
    assert scoped_prod["load_file_attempts"] == []
    assert scoped_prod["bates_gaps"]["computed"] is False

    # Transcript window uses interval overlap (includes cue [90, 105] when querying [95, 110]).
    full_rt = MatterToolRuntime(backend.matter_id, "eval-defender", backend)
    overlap = full_rt.invoke("read_transcript_window", {"document_id": "ext-transcript", "start_seconds": 95, "end_seconds": 110})
    assert overlap["count"] == 1 and overlap["cues"][0]["start_seconds"] == 90.0

    # inspect_production honors load_file_document_id: rejects missing IDs and does not report gaps for non-load-file docs.
    with pytest.raises(AssistantToolError, match="not found"):
        full_rt.invoke("inspect_production", {"load_file_document_id": "does-not-exist"})
    wrong_doc = full_rt.invoke("inspect_production", {"load_file_document_id": "ext-ledger"})
    assert wrong_doc["bates_gaps"]["computed"] is False and wrong_doc["bates_gaps"]["facts"] == []
