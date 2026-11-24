# Live Case Assistant and synthesis evaluation runner (Slice 7)

## Problem

Before relying on any local or operator-approved model endpoint for real
criminal-defense discovery review, teams need an objective, repeatable harness
that runs the real tool-using `CaseAssistant` over a multi-hundred-document
synthetic corpus and scores cross-document reasoning, exact math provenance,
citation coverage, gap reporting, and honest abstention.

## Root cause

Existing `defense_eval.py` evaluates single-turn retrieval and extractive
grounded answering, not multi-turn `CaseAssistant` tool loops, `CalculationReceipt`
provenance, competing-account detection, or abstention traps across the 405-doc
`stock_case_gold` corpus.

## Approach

Add `src/case_intelligence/case_assistant_eval.py` under the `"evals"` layer in
`tests/snapshots/import_layers.json`:

1. **Seeded synthetic corpus & exact answer key (`build_stock_eval_suite`):**
   Renders `stock_case_gold.build_case(seed)` via `stock_case_render.documents`
   plus one unreadable scan (`damaged-scan.pdf`) behind a `MatterToolBackend`
   and derives exact ground-truth expectations for four defense review tasks:
   - `ownership_threshold`: cross-account ownership `> 10%` after the buyback
     (exact percentage, `CalculationReceipt`, filing + statement citations, and
     unreadable-file gap notice).
   - `buyback_to_sale_days`: exact `44` calendar days (`days_between` receipt).
   - `associate_contradiction`: witness interview vs. email instruction conflict
     (`competing_accounts` + both source kinds cited).
   - `unrecorded_market_price_trap`: honest abstention (`answerable=False`) when
     discovery contains no share market prices.
2. **Deterministic 100-point scorer (`score_eval_case`):** Scores answer/math
   accuracy (40 pts), proportional required-source citation coverage (25 pts),
   `CalculationReceipt` provenance (20 pts), and honesty/gap discipline (15 pts),
   with an automatic `0` on falsely answered abstention traps.
3. **CLI (`python -m case_intelligence.case_assistant_eval`):** Supports
   `--backend openai|ollama|scripted`, `--url`, `--model`, `--api-key`, `--seed`,
   and overwrite-protected `--out-json` / `--out-md` reports.

## Vision check

Directly tests the canonical `VISION.md` stock-ownership benchmark against the
real single assistant without capping answers or weakening any check.

## What it could break

- `tests/test_import_boundaries.py`: registers `case_assistant_eval` in `evals`
  (no runtime module imports it).
- `tests/test_generator_no_tools.py`: T7 adds `case_assistant_eval`.

## Proving test

`tests/test_case_assistant_eval.py` runs `case_assistant_eval.main` end-to-end
on `seed=7` with the scripted reference assistant (asserting `100.0/100`, valid
JSON/Markdown artifacts, and overwrite protection) and scores a guessing/flawed
assistant (asserting `0` on the abstention trap and reduced score).
