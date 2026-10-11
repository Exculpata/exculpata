# 10: Make review budgets and coverage visible

Status: implemented on `main` as
[#40](https://github.com/Exculpata/exculpata/pull/40). See
[investigation budgets](../INVESTIGATION_BUDGETS.md). No dependencies.

## Finding and outcome

The investigation path requests 30 primary results, but `HybridRetriever.search`
caps its return at 20. Default research has five preset passes, selects up to 12
passages per pass, and sends at most 12 to final synthesis. The generator allows
48,000 total evidence characters and 1,200 output tokens. These separate limits
make a vague label such as deep review misleading.

## Small implementation

Create one explicit review-budget object consumed by orchestration, retrieval,
generation, and progress reporting. First preserve current resource limits while
removing the contradictory requested/effective values. Reject invalid budgets
or report the effective bound before running; do not silently clamp.

Persist requested/effective passes, candidates, unique evidence, synthesis
inputs, truncation, and stop reason. Present readable counts: searched sources,
selected passages, analyzed units, and unavailable material. Do not convert
selected-source counts into a percentage of pages read.

## Code and acceptance

Start with `workbench.py::_process_research_job`, `_answer_search`,
`review_bench_v2.py::HybridRetriever`, `generation.py` limits, progress templates,
and work-product exports.

Synthetic checks request 30 results from 50 matches and verify an explicit
effective budget or validation error, never an unexplained 20. Test fewer
available results, repeated passages across passes, cancellation, restart,
and UI/export agreement. Preserve old saved runs with explicit unknown budget
metadata rather than inventing historical counts. Persisted format changes need
compatibility/restore evidence. This is not the slice that raises limits.
