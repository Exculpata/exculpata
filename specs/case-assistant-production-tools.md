# Bates-aware list_sources and read-only inspect_production Case Assistant tool

## Kickoff

- Goal restated: Make the single Case Assistant production- and Bates-aware by populating produced Bates ranges and team identifiers in `list_sources` (with Bates query matching) and adding a read-only allowlisted `inspect_production` tool that reports document/page counts, failed/unreadable files, stored load-file attempts (DAT/OPT/LFP/DII), and deterministic Bates-gap findings without inventing a separate mode.
- Expected files: `specs/case-assistant-production-tools.md`, `src/case_intelligence/case_assistant_tools.py`, `src/case_intelligence/case_assistant.py`, `src/case_intelligence/workbench_assistant.py`, `docs/CASE_ASSISTANT.md`, `tests/test_case_assistant_production_tools.py`.
- Acceptance check: Synthetic unit and HTTP tests prove (1) `list_sources` populates exact produced Bates ranges and team-assigned IDs (keeping them distinct) and filters by Bates query; (2) `inspect_production` returns counts by media type, failed/unreadable sources, stored load-file attempts, and deterministic Bates-gap findings from `production_summary` when a CSV load file is available; (3) `inspect_production` obeys live matter authorization, `allowed_document_ids` scope, and T7 no-code-execution / allowlist payload checks; (4) `test_generator_no_tools.py`, `test_import_boundaries.py`, `test_db_access_ratchet.py`, `test_case_assistant_tools.py`, `test_workbench_assistant.py`, and `test_case_assistant_production_tools.py` pass.
- Issue link: [Volume import #279](https://github.com/Exculpata/exculpata/issues/279) and [Production summary #273](https://github.com/Exculpata/exculpata/issues/273).

## Problem and root cause

`SourceSummary` in `case_assistant_tools.py` already declares a `bates` field, but `WorkbenchToolBackend.list_sources` never populates it. In addition, the Case Assistant has no read-only tool to inspect production overview counts, stored DAT/OPT/LFP/DII import coverage, or Bates-gap findings, forcing attorneys to leave the assistant to answer basic production-completeness questions.

## Approach

1. Add `"inspect_production"` to `ALLOWED_TOOLS`, `TOOL_DEFINITIONS`, and `MatterToolRuntime._dispatch` (`case_assistant_tools.py`), taking optional `load_file_document_id` and `include_gaps` (boolean, default `True`), checking `allowed_document_ids` scope before calling the backend.
2. Add `"inspect_production"` to `_TOOL_STAGE_LABELS` and `_SYSTEM_PROMPT` in `case_assistant.py`, and record unavailable sources reported by `inspect_production` so record-gap honesty carries through verification.
3. In `WorkbenchToolBackend` (`workbench_assistant.py`):
   - Populate `SourceSummary.bates` in `list_sources` via `ProductionRepository.document_records` (for matched completed production records, failing closed on conflicting ranges) and `IdentifierRepository.read` (prefixed `Team ID: ...` so team identifiers stay distinct from produced Bates), and match `query` against `bates` as well.
   - Implement `inspect_production(matter_id, *, load_file_document_id="", include_gaps=True, document_ids=None)` reusing `ProductionRepository.attempts`, `store.documents`, and `production_summary.build_summary` (scoped strictly to authorized `document_ids`), returning bounded counts, unavailable sources, load-file attempts, and deterministic Bates-gap findings with an explicit honesty notice.
4. Update `docs/CASE_ASSISTANT.md` tool table to document `inspect_production` and Bates-aware `list_sources`.

## Vision check

Directly follows `VISION.md` ("Never add a mode or a single-use feature. Make the one assistant better instead.") and `docs/IDEAL.md` (`A1`, `A2`, `A3`, `E2`, `E3`).

## What it could break

- `tests/test_generator_no_tools.py`: validates `ALLOWED_TOOLS` and T7 no-dynamic-execution rules; `inspect_production` is strictly read-only with zero dynamic execution.
- `tests/test_import_boundaries.py`: `case_assistant_tools` remains stdlib-only; `workbench_assistant` (`app_shell`) may import `ProductionRepository`, `IdentifierRepository`, and `production_summary`.

## Proving test

`tests/test_case_assistant_production_tools.py` tests Bates population and filtering in `list_sources`, end-to-end `inspect_production` over a synthetic production with a DAT attempt, CSV Bates-gap file, failed PDF, source-set scoping, and unauthorized/cross-matter denial.
