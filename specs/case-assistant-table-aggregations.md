# Case Assistant `query_table` grouped aggregations, `avg`, and `sort_by`

## Kickoff

- **Goal (user-visible outcome):** Extend the single Case Assistant's read-only `query_table` tool to support deterministic `group_by` aggregations, exact `Decimal` `avg`, and deterministic `sort_by` / `sort_order` over CSV/TSV/XLSX discovery sources, emitting auditable, source-cited `CalculationReceipt`s per group so financial and ledger questions require fewer turns and remain 100% grounded.
- **Expected files to touch:**
  - `specs/case-assistant-table-aggregations.md`
  - `src/case_intelligence/case_assistant_tools.py`
  - `src/case_intelligence/case_assistant.py`
  - `docs/CASE_ASSISTANT.md`
  - `tests/test_case_assistant_rich_tools.py`
- **Acceptance check:**
  1. Synthetic unit and end-to-end tests in `tests/test_case_assistant_rich_tools.py` prove `group_by` (`sum`, `count`, `min`, `max`, `avg`), ungrouped `avg`, numeric and lexical `sort_by` (`asc`/`desc`), auditable per-group `CalculationReceipt` verification inside `CaseAssistant`, and fail-closed errors on unknown columns, invalid `sort_order`, missing `aggregate_op` with `group_by`, non-numeric values, and group-count caps.
  2. New tests fail on base commit `6a4a6c1` and pass after the change.
  3. `tests/test_case_assistant_rich_tools.py`, `tests/test_case_assistant_tools.py`, `tests/test_case_assistant.py`, `tests/test_generator_no_tools.py`, `tests/test_import_boundaries.py`, `tests/test_source_size_ratchet.py`, `ruff`, and `scripts/publication-check.py --skip-history` pass.
- **Issue link:** Aligned with [VISION.md](../VISION.md) ("Make the one assistant better") and [CONTRIBUTOR_BACKLOG.md](../docs/CONTRIBUTOR_BACKLOG.md).

## Problem and root cause

`query_table` currently supports single-column `count`, `sum`, `min`, and `max` over all matched rows, with no grouping, no average, and no sorting. Multi-account ledger or transaction questions force the Case Assistant to issue repeated per-account filter calls and manual `calculate` steps, spending turn budget and risking partial coverage.

## Approach

1. Add `"avg"` to `ALLOWED_TABLE_AGG_OPS` and add optional parameters `group_by` (string), `sort_by` (string), `sort_order` (`"asc"` | `"desc"`), and `precision` (`0..12`, default `6`) to the `query_table` tool schema in `src/case_intelligence/case_assistant_tools.py`.
2. In `query_table_rows`:
   - Validate `sort_by` (must exist in `columns`) and `sort_order` (`"asc"` or `"desc"`), sorting matched rows deterministically (exact `Decimal` order when all non-empty values parse as `Decimal`, otherwise case-insensitive text with blanks sorted last).
   - Support exact `Decimal` `"avg"` (`total / count` quantized to `precision`) alongside `count`/`sum`/`min`/`max`.
   - When `group_by` is provided, require a valid `group_by` column and `aggregate_op`; group matched rows by trimmed cell value (`"(blank)"` for empty cells), enforce `MAX_TABLE_GROUPS = 100` and total `MAX_CALC_INPUTS = 500`, and emit a grounded `CalculationReceipt` per group (`calc-N`, `calc-N+1`, ...) with every contributing row's `SourceRef`.
3. Register all per-group `CalculationReceipt`s in `MatterToolRuntime._receipts` and register their contributing input values in `CaseAssistant`'s `inspected_texts` (`src/case_intelligence/case_assistant.py`) so findings citing any group's `receipt_id` pass full premise/derivation verification.
4. Update `docs/CASE_ASSISTANT.md` tool table description for `query_table`.

## Vision check

Follows [VISION.md](../VISION.md) and [IDEAL.md](../docs/IDEAL.md) (`A1`, `E2`, `E3`): improves the single assistant's deterministic table reasoning with zero `eval`/`exec`, no new mode, and complete source-span receipts.

## What it could break

- `tests/test_generator_no_tools.py`: validates `ALLOWED_TOOLS` and T7 static no-execution rules; `query_table` remains allowlisted and strictly read-only.
- Existing `query_table_rows` callers omitting `group_by`/`sort_by`/`sort_order`/`precision`: all new parameters are optional with backward-compatible defaults and return shape.
