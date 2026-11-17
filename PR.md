# PR #554: withdraw changed production sources

## Kickoff

- Goal and user-visible outcome: merge main into the prescribed PR baseline and ensure changed load-file sources stop contributing current associations and completed production counts.
- Expected files: main merge contents; production/service.py (repository.py if necessary), tests/test_discovery_review_image_load_files.py, and PR.md.
- Acceptance check: preserve r1's seven regression cases; reproduce failure before the condition fix; pass the three required files and 20 changed-bytes repetitions with four workers; run local parity on the committed merge; retain the 30% duration rule and 125 s / 135 s ceilings.
- Issue: https://github.com/Exculpata/exculpata/issues/503; fix-up: https://github.com/Exculpata/exculpata/pull/554.

The supplied r2 scope matches this goal. Main and the remote PR baseline were fetched and verified; issue #503 is unassigned with no fetched comments, and #554 is draft with the previous job receipt. Later slices remain excluded. Kickoff recorded before the new product correction; r1's existing regression tests are retained.

## Correction and validation

CI-ONLY MISS: branch-only parity missed main retaining changed catalog rows; merged reconciliation now withdraws them.

Fix level: product invariant. The existing stale-attempt cleanup now also applies when the catalog source_state is not ready. This is a deterministic source-withdrawal correction, not a timing change; main's pending/restart behavior is retained.
Regression baseline: failed hosted merge 20466b8e72f5eccc2c3205935c2880af4fe2778e. All seven regression cases fail on its service implementation and pass with the correction; r1's assertions are preserved exactly.
Validated product merge: 4993707a7bfba875a3956e7e8a8f5e3cfb7918ad, with parents dd7b10b02ccb8b14471eead226a7cb76edb2157a and main 718469390d50c49465fd7e7f64d6661c4ed91363. Compared with the failed hosted merge, only service.py, the regression file and this record differ. The final follow-up changes only this validation record.

- Required files: test_discovery_review_image_load_files.py, test_source_image_load_files.py and test_discovery_review_dat_opt.py: **101 passed**, 72.04 s.
- Original changed-bytes test, 20 independent fixture instances with `-n 4 --dist loadgroup`: **20 passed**, 10.82 s; it also passed in full application shard 1.
- Main's pending/restart tests, fixture-write/inventory checks and DB-access ratchet: **2,064 passed**.
- Full local parity on the clean committed merge: **45 ran, 0 failed, 30 expected NOT RUN, 0 unexpected; GREEN OVERALL**. Application shards: 9,224 and 15,102 passed.
- Unchanged duration gates passed: lifecycle resume **101.98 s / 125 s**; row_budget[10000] **87.66 s / 135 s**. All other reported phase totals satisfy the 30% rule; required-file maximum 40.61 s, repetition maximum 2.50 s.
- Added-lines private-terms scan, candidate tree/reachable-history publication scan, lint and diff checks passed. No new skip, xfail, retry, longer wait, timeout change, workflow or parity-file change.

Reproduction commands, repeat helper, revision/blob record, before/after logs, focused summary and full parity report/logs are retained in `r2-evidence/` beside the worktree. The repeat helper parameterizes the original test; it never retries failures. `last.md` is excluded and uncommitted.

## Readiness

Scope and parent-contract blockers are resolved; the correction and local validation are complete. Ready for launcher validation and review. Nothing was pushed; maintainer authorization is still required for a merge to main.
