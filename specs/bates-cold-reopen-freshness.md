# Cold-reopen dependency freshness in `ProductionRepository.document_bates_ranges`

## Kickoff

- **Goal (user-visible outcome):** Ensure persisted produced Bates ranges served by `ProductionRepository.document_bates_ranges` (and its callers in citation propagation and Case Assistant `list_sources`) enforce the same dependency freshness contract as `ProductionService.attempt_current` / `reconcile_attempt`: if a matter is reopened cold before production re-validation runs, or if a companion or target document in the production root changes on disk or in catalog state outside the load file itself, stale produced Bates ranges are withheld until re-indexed.
- **Expected files to touch:**
  - `specs/bates-cold-reopen-freshness.md`
  - `src/case_intelligence/modules/discovery_review/production/matching.py`
  - `src/case_intelligence/modules/discovery_review/production/repository.py`
  - `src/case_intelligence/modules/discovery_review/production/service.py`
  - `src/case_intelligence/workbench.py`
  - `src/case_intelligence/workbench_assistant.py`
  - `tests/test_bates_citation_propagation.py`
- **Acceptance check:**
  1. Synthetic regression tests in `tests/test_bates_citation_propagation.py` import a load file, persist a complete production attempt, then (a) reopen the matter cold and verify `document_bates_ranges`, `bench.search` citations, and Case Assistant `list_sources` withhold stale Bates ranges until re-indexed, and (b) mutate/add/remove a companion or target file in the same production root and verify stale Bates ranges are withheld until re-indexed.
  2. Regression tests fail on base commit `6a4a6c1` and pass after the fix.
  3. `tests/test_bates_citation_propagation.py`, `tests/test_case_assistant_production_tools.py`, `tests/test_discovery_review_dat_opt.py`, `tests/test_import_boundaries.py`, `tests/test_db_access_ratchet.py`, `tests/test_source_size_ratchet.py`, `ruff`, and `scripts/publication-check.py --skip-history` pass.
- **Issue link:** https://github.com/Exculpata/exculpata/issues/601

## Problem and root cause

`ProductionRepository.document_bates_ranges` checks `p.status='complete'`, the load file's `workbench_source_catalog` `version_id`/`source_state='ready'`, `not deps.get('paused')`, and `document_status == 'complete'`. It does not verify:
1. That the target document itself still exists in `workbench_source_catalog` with `source_state='ready'` and `origin='upload'`, or that load-file provenance still matches `p.root_key`, or that `deps.get('state') == 'complete'`.
2. That the live source store's `source_changes.epoch` still matches `deps['epoch']` (which changes on every cold reopen of a matter until `ProductionService` re-validates the attempt).
3. That the root's live candidate dependency SHA-256 hash (`deps['hash']`, computed over each candidate's `(version_id, digest, display_name, relative_path, media_type, state, origin)`) still matches the live store's candidates when a companion or target file is added, removed, or mutated outside the load file itself.
4. Even when called without a live `store`, whether any companion/target document in the same `root_key` was updated in `workbench_source_catalog` after the attempt completed or removed from the catalog.

## Approach

1. Share `source_input_identity(item)` and `attempt_dependency_hash(candidates_iter, read_snapshot)` in `production/matching.py` (already imported by both `repository.py` and `service.py`, keeping zero circular imports and `repository.py` under the 1,500-line ratchet).
2. Update `ProductionRepository.document_bates_ranges(matter_id, document_ids=None, *, store=None)` and `document_bates_label(..., store=None)`:
   - Join target document `t` in `workbench_source_catalog` (`t.source_state='ready' AND t.origin='upload'`) and require `c.origin='upload'` on the load file.
   - Require `deps.get('state') == 'complete'` and `not deps.get('paused')`.
   - Validate once per `attempt_id`: load-file provenance matches `p.root_key`; if `store` is supplied, require `deps.get('epoch') == f"production-v2-{store.source_changes.epoch}"` and recompute the exact `reconcile_attempt` SHA-256 over `self.candidates(matter_id, root_key, ...)` via `store.source_changes.read`, requiring it to equal `deps.get('hash', '')`.
   - If `store` is not supplied, also verify no candidate in `p.root_key` has `updated_at > p.updated_at` in `workbench_source_catalog` when `p.updated_at` is set.
3. Forward the already-resolved `PilotStore` from `ReviewBench._citation` (`workbench.py`, net 0 lines added) and `WorkbenchToolBackend._document_bates_map` (`workbench_assistant.py`).

## Vision check

Preserves [IDEAL.md](../docs/IDEAL.md) `E1` and `E3`: never serve stale production citations after a cold reopen or unindexed companion/target change.

## What it could break

- `tests/test_bates_citation_propagation.py`: assigns a Team ID to an unrelated document after `complete_listing` and checks `document_bates_ranges` without `store` and via `bench.search` with `store`; since Team ID assignment does not change `source_changes.epoch` or root candidate snapshots, valid fresh ranges remain served.
- `tests/test_source_size_ratchet.py`: `production/repository.py` stays <= 1,500 lines and `workbench.py` stays <= 16,921 lines.
- `tests/test_import_boundaries.py`: no new modules or cross-layer imports added.
