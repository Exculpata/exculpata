# Case assistant workbench integration and expandable work trace (Slice 3)

## Problem

Slices 1–2 delivered the read-only matter tool runtime (`case_assistant_tools.py`)
and multi-turn tool loop + verifier (`case_assistant.py`), but the running web
workbench (`workbench.py`) still routes every Ask job through the single-pass,
12-passage `GroundedGenerationService` and has no UI for calculation receipts,
record gaps, competing accounts, or tool traces.

## Root cause

1. `src/case_intelligence/workbench.py:4883` (`_process_answer_job`) always runs
   single-pass retrieval followed by `self.generator.answer(...)`.
2. No `app_shell` adapter binds `MatterToolBackend` to `WorkbenchService`
   (`PilotStore`, hybrid search, exact search, and saved case knowledge).
3. `templates/workbench_assistant.html` and `workbench_workspace.html` have no
   section to render `CalculationReceipt`s, gaps, competing accounts, or traces.

## Approach

Add an `app_shell` integration module (`src/case_intelligence/workbench_assistant.py`)
keeping `workbench.py` well within its `source_line_counts.json` ceiling:

1. **Feature-gated workbench dispatch (`CASE_INTELLIGENCE_CASE_ASSISTANT=1`):**
   When enabled on standard Ask jobs, `_process_answer_job` delegates to
   `run_workbench_assistant_job(...)`, leaving existing focused-answer paths and
   recorded-context dispatches unchanged.
2. **Live `WorkbenchToolBackend`:** Implements `MatterToolBackend` over the
   authorized `WorkbenchService`: live `workspace.membership` checks, `PilotStore`
   source listing (explicitly surfacing non-searchable/failed files), hybrid
   (`bench.search`) and exact (`bench.exact_search`) discovery search, full-page
   `read_source` via `iter_parsed_units()`, and `get_case_knowledge` over
   `MatterKnowledgeService` and saved case notes. Every inspected source unit is
   indexed as a canonical `WorkbenchCitation` with a real `support_token` so
   saved answers work seamlessly with source opening, notes, reports, and exports.
3. **Expandable work-trace UI (`templates/workbench_assistant_trace.html`):**
   Shared partial included in both `workbench_workspace.html` and
   `workbench_assistant.html` showing verified calculations, record gaps,
   competing accounts, and tool steps when `message.payload.case_assistant` is present.

## Vision check

Matches `VISION.md`: wires the single tool-using assistant directly into the
real workbench, keeps exact computed numbers and citations clickable, surfaces
record gaps honestly, and preserves all authorization and egress checks.

## What it could break

- `tests/test_import_boundaries.py`: registers `workbench_assistant` as `app_shell`.
- `tests/snapshots/source_line_counts.json`: `workbench.py` grows by < 10 lines.
- Existing Ask tests: unchanged when `CASE_INTELLIGENCE_CASE_ASSISTANT` is unset.

## Proving test

`tests/test_workbench_assistant.py` boots `create_workbench_app(..., auth_mode="test")`
with `CASE_INTELLIGENCE_CASE_ASSISTANT=1`, uploads synthetic buyback + position
records plus a damaged PDF, posts `/matters/{slug}/ask`, and proves the real
background worker drives `list_sources` -> `search_discovery` -> `read_source`
-> `calculate` and renders the verified `> 10%` finding, clickable citations,
calculation receipt, gap notice, and tool trace in HTML; unauthorized access fails closed.
