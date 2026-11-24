# Case assistant UI polish, live step labels, and inline proposal saves (Slice 6)

## Problem

Slices 3–5 added the expandable work trace, calculation receipts, record gaps,
and reviewer-gated proposal cards, but the markup was unstyled, tool steps
displayed raw snake_case tool names during polling, and clicking **Save to case
memory** forced a full page reload.

## Root cause

1. `src/case_intelligence/case_assistant.py:258` emitted `"Running <tool_name>..."`
   instead of attorney-friendly stage messages.
2. `src/case_intelligence/static/case-intelligence.css` had no styling rules for
   `.assistant-work-trace` or `.assistant-proposals`.
3. `src/case_intelligence/static/case-intelligence.js` had no progressive-enhancement
   JSON submit handler for `form[data-assistant-proposal-form]`.

## Approach

1. **Human-readable live investigation stages (`case_assistant.py`):** Map every
   allowlisted tool in `_TOOL_STAGE_LABELS` to clear, plain-language progress
   text reported through `stage_callback` into existing answer-job polling.
2. **Accessible CSS styling (`case-intelligence.css`):** Style `.assistant-work-trace`,
   `.assistant-trace-section`, `.assistant-proposals`, `.assistant-proposal-card`,
   category badges (`case_theory`, `contradiction`, `timeline_event`,
   `disclosure_review`), and status feedback using existing theme tokens.
3. **Progressive-enhancement inline save (`workbench_assistant_trace.html` + `case-intelligence.js`):**
   Add `data-assistant-proposal-form` and `role="status"` live regions on each
   proposal card and attach one delegated `submit` handler in
   `case-intelligence.js` that POSTs with `Accept: application/json`, updates
   button and status copy inline without reloading, and surfaces `409` stale-source
   errors cleanly—while preserving full non-JS form POST fallback.

## Vision check

Matches `VISION.md`: makes the single assistant's work, math, gaps, and
reviewer-gated memory updates legible and fast for attorneys without adding modes.

## What it could break

- `tests/test_rename_javascript_consumers.py`: JS syntax and static consumer checks.
- `tests/test_workbench_assistant.py` and `tests/test_case_assistant_proposals.py`:
  preserved all existing text and attributes.

## Proving test

`tests/test_case_assistant_ui.py` verifies human-readable tool stage messages,
rendered proposal/trace markup on both Ask and Assistant dock routes, CSS rule
presence, and valid JavaScript syntax via `node --check`.
