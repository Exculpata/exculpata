# Case assistant architecture and matter-scoped tool runtime

## Problem

Exculpata's answering path behaves like a single-turn, 12-passage Q&A widget
rather than a full-fledged case assistant. Attorneys cannot ask cross-document
questions (reconstructing share ownership across accounts, splits, and filings;
comparing witness statements; or surfacing gaps from unreadable files) and get a
reasoned, source-cited analysis that shows its work.

## Root cause

Four constraints built for small bundled 4B/9B models now block `VISION.md`:

1. `src/case_intelligence/generator_schema.py:27` (`refuse_tool_request`) blocks
   all tool payloads; `ask_router.py:68` splits prompts into regex modes;
   `investigation_planner.py:42` only queues blind regex-anchor queries.
2. `src/case_intelligence/generation.py:899-901` drops any claim with a number
   absent verbatim from raw text; lines `907-918` require 60% literal token overlap.
3. `src/case_intelligence/review_budget.py:7-18` and `generation.py:51-62` cap
   answers at 8 claims, 12 passages, 1,200 output tokens, and `num_ctx: 8192`.
4. Saved entities, assertions, timelines, and case theory (`case_theory.py`,
   `matter_knowledge.py`) are not queryable by the assistant.

## Approach

Deliver one Case Assistant in four slices (each <= 500 lines), preserving
`docs/IDEAL.md` authority, isolation, and evidence-integrity invariants:

- **Slice 1 (this PR) — Matter-scoped tool runtime (`case_assistant_tools.py`):**
  Provide five read-only allowlisted tools (`list_sources`, `search_discovery`,
  `read_source`, `get_case_knowledge`, `calculate`) behind `MatterToolBackend`
  in `modules.answers`. Every call checks live `authorize(matter_id, actor_id)`
  before touching data, enforces document-set scope, reports failed/unreadable
  sources, and records an auditable trace of calls and `SourceRef`s. `calculate`
  runs exact `Decimal` and date math (`sum`, `subtract`, `multiply`, `divide`,
  `percent`, `days_between`, `running_balance`) with zero `eval`/`exec` and
  returns a source-cited `CalculationReceipt`.
- **Slice 2 — Tool-using assistant loop & premise/derivation verifier:**
  Add a matter-scoped OpenAI/Ollama tool-loop runner (keeping `refuse_tool_request`
  on legacy paths and preserving T7 no-code-execution). Verify raw premises against
  originals and computed numbers against `CalculationReceipt`s instead of dropping them.
- **Slice 3 — Workbench & live synthesis integration:**
  Bind `MatterToolBackend` in `app_shell` inside `AnswerCoordinator` with live
  stage progress, expandable work traces, and `stock_case_gold` / `synthesis_eval` scoring.
- **Slice 4 — Reviewer-gated case memory & proposals (`#278`):**
  Read and emit cited proposal cards for case theory, timelines, witness
  contradictions, and disclosure-review queues; attorneys confirm every write.

## Vision check

Fits `VISION.md`: one assistant choosing searches, reads, and math via narrow
tools; exact `CalculationReceipt`s preserve computed numbers; unavailable files
are flagged honestly; execution stays local and fail-closed.

## What it could break

- `tests/test_import_boundaries.py`: registers `case_assistant_tools` in
  `modules.answers` (stdlib-only, zero cross-module edges or cycles).
- `tests/test_generator_no_tools.py`: Slice 1 touches no chat payloads and uses
  zero dynamic execution. Existing routes and storage schemas are unchanged.

## Proving test

`tests/test_case_assistant_tools.py` renders `stock_case_gold.build_case(seed=7)`
via `stock_case_render.documents`, runs `list_sources` -> `search_discovery` ->
`read_source` -> `calculate`, and proves: (1) combined controlled holdings vs.
post-buyback shares compute `> 10%` with exact `CalculationReceipt`s and source
citations; (2) unreadable files appear in `list_sources`; (3) unauthorized
actors, cross-matter reads, unknown tools, division by zero, and `NaN` fail closed.
