# Propagate produced Bates and team identifiers into citations and search results

## Kickoff

- Goal restated: Surface produced Bates ranges (from completed, uniquely matched production load files) and team-assigned identifiers on `WorkbenchCitation` objects, serialized citation payloads, and rendered workspace citation chips without altering `source_name`, `location`, `support_token`, or renumbering admitted originals.
- Expected files: `specs/bates-citation-propagation.md`, `src/case_intelligence/modules/discovery_review/production/repository.py`, `src/case_intelligence/workbench.py`, `src/case_intelligence/templates/workbench_workspace.html`, `tests/test_bates_citation_propagation.py`.
- Acceptance check: Synthetic unit and HTTP tests prove that (1) documents matched by a completed DAT production carry `Bates: <begin>` or `Bates: <begin>–<end>` (exact produced casing) on search hits and answer citations; (2) documents without produced Bates but with a team-assigned identifier carry `Team ID: <id>`; (3) produced Bates takes precedence over Team ID and conflicting produced ranges fail closed to empty; (4) `source_name`, `location`, `support_token`, and saved-work report verification remain unchanged; (5) cross-matter citations never leak Bates labels across matters.
- Issue link: [Volume import #279](https://github.com/Exculpata/exculpata/issues/279).

## Problem and root cause

Discovery review already stores produced Bates ranges in `workbench_discovery_dat_record` (via `ProductionRepository`) and team-assigned identifiers in `workbench_discovery_identifier` (via `IdentifierRepository`). However, `WorkbenchService._citation` never reads either repository, so attorneys reviewing search hits, answers, or Case Assistant citations see only filenames and page numbers instead of the Bates labels needed for filings and witness examination.

## Approach

1. Add a bounded, read-only method `document_bates_ranges(matter_id, document_ids=None)` to `ProductionRepository` (`modules/discovery_review/production/repository.py`) that reads exact produced `begin_bates`/`end_bates` strings from `semantic_json` on completed, matched records matching the current attempt generation. Fail closed (omit label) if multiple conflicting produced ranges exist for the same document.
2. Add optional `bates_label: str = ""` to `WorkbenchCitation` (`workbench.py`) and resolve it read-only in `_citation(...)` via `ProductionRepository.document_bates_ranges` and `IdentifierRepository.read_documents` behind `ModuleDatabase(self.workspace)` (no raw cursor reads in `workbench.py`, satisfying `test_db_access_ratchet.py`). Prefix produced labels as `Bates: ...` and team-assigned identifiers as `Team ID: ...` so provenance stays unambiguous.
3. Include `"bates_label"` in `staff_payload()` and `_workflow_citation_payload()` only when non-empty (preserving full backward compatibility for existing payloads and saved-work verification) and restore it in `_workflow_citation(...)`.
4. Render `{% if citation.bates_label %} · {{ citation.bates_label }}{% endif %}` on citation chips in `workbench_workspace.html`.

## Vision check

Directly serves `VISION.md` and `docs/IDEAL.md` (`A1`, `A2`, `E1`, `E2`): brings real produced Bates citations into search and assistant answers through the existing repository boundaries without altering original identities, support tokens, or renumbering originals.

## What it could break

- Existing `WorkbenchCitation` callers and serialized citation tests: `bates_label` defaults to `""` and is omitted from dict payloads when empty, preserving exact compatibility.
- `tests/test_db_access_ratchet.py` and `tests/test_import_boundaries.py`: all SQL lives inside `ProductionRepository` and `IdentifierRepository`.

## Proving test

`tests/test_bates_citation_propagation.py` exercises real DAT production intake, team-identifier assignment, search citations, saved answer citations, workspace HTML rendering, conflicting-range fail-closed omission, and cross-matter isolation.
