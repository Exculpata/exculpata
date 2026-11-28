# Expanded synthetic multi-document scenarios for `case_assistant_eval`

## Kickoff

- **Goal (user-visible outcome):** Expand `case_assistant_eval.py` with an opt-in `--suite extended` synthetic multi-document benchmark (preserving the default 4-case `stock-ownership` suite byte-for-byte) that exercises Case Assistant's rich discovery tools end-to-end: Bates-gap / production inspection (`inspect_production`), email-thread + attachment-gap synthesis (`read_email_thread`), timestamped transcript-window contradictions (`read_transcript_window`), and structured CSV ledger aggregation (`query_table`).
- **Expected files to touch:**
  - `specs/case-assistant-eval-extended-scenarios.md`
  - `src/case_intelligence/case_assistant_eval.py`
  - `docs/CASE_ASSISTANT.md`
  - `tests/test_case_assistant_eval.py`
- **Acceptance check:**
  1. Synthetic tests in `tests/test_case_assistant_eval.py` prove:
     - `--suite extended --backend scripted` runs all 8 cases (4 canonical stock cases + 4 new rich-tool cases) to `100.0/100`, verifies every new case's citations, calculation receipts, gap notices, and competing accounts, and preserves default `--suite stock-ownership` (4 cases, `100.0/100`).
     - Scorer penalizes missing gap notices on attachment/Bates gaps, missing competing accounts on transcript contradictions, and missing calculation receipts on table aggregations.
     - `InMemoryEvalBackend` enforces live matter isolation and `allowed_document_ids` scoping on `read_email_thread`, `read_transcript_window`, and `inspect_production`.
  2. New tests fail on base commit `6a4a6c1` and pass after implementation.
  3. `tests/test_case_assistant_eval.py`, `tests/test_generator_no_tools.py`, `tests/test_import_boundaries.py`, `tests/test_source_size_ratchet.py`, `ruff`, and `scripts/publication-check.py --skip-history` pass.
- **Issue link:** Aligned with [CONTRIBUTOR_BACKLOG.md](../docs/CONTRIBUTOR_BACKLOG.md) ("Case Assistant evaluation") and [VISION.md](../VISION.md).

## Problem and root cause

`case_assistant_eval.py` currently evaluates only 4 stock-case scenarios over plain text/PDF search and `calculate`. Now that Case Assistant is default-on (#600), Bates/production-aware (#599), and supports rich tools (`query_table`, `read_email_thread`, `read_transcript_window`, `inspect_production`), the eval harness lacks synthetic multi-document scenarios testing those tools, attachment-gap honesty, Bates-gap detection, and transcript contradictions.

## Approach

1. Extend `InMemoryEvalBackend` in `src/case_intelligence/case_assistant_eval.py` with optional synthetic thread, transcript, and production-audit fixtures backing `read_email_thread`, `read_transcript_window`, and `inspect_production` (strictly respecting `matter_id` and `document_ids` scoping).
2. Add `build_extended_eval_suite(seed=7)` combining the 4 canonical stock-ownership cases with 4 deterministic synthetic scenarios:
   - `bates_gap_production_audit` (`inspect_production`: missing Bates `PROD0003`, unreadable scan gap, `production_summary` receipt)
   - `email_thread_attachment_gap` (`read_email_thread`: 3-message thread + unextracted attachment gap notice)
   - `transcript_window_contradiction` (`read_transcript_window` + `query_table`: timestamped witness denial contradicting CSV wire ledger, competing accounts)
   - `table_ledger_aggregation` (`query_table`: exact `Decimal` wire total `20000.75` for `stk-0` with auditable `CalculationReceipt`)
3. Add optional `required_gap_pattern` to `EvalCase` so scenarios like `bates_gap_production_audit` and `email_thread_attachment_gap` verify scenario-specific gap disclosure alongside unreadable-scan gaps.
4. Add `--suite {stock-ownership,extended}` CLI flag (default `stock-ownership`) and extend `_ScriptedEvalModel` to prove all 8 cases end-to-end.
5. Document `--suite extended` in `docs/CASE_ASSISTANT.md`.

## Vision check

Follows [VISION.md](../VISION.md): measures the one Case Assistant on real cross-document synthesis, exact receipts, citations, and honest gap reporting using purely synthetic fixtures.

## What it could break

- Existing callers of `build_stock_eval_suite`, `run_evaluation`, and `main(["--backend", "scripted", ...])` expecting `suite == "stock-ownership"` and 4 cases: default remains `"stock-ownership"` with identical behavior.
- `tests/test_generator_no_tools.py`: `case_assistant_eval` is in `GENERATOR_MODULES` and scanned by T7 static no-execution rules; zero dynamic execution or disallowed imports introduced.
