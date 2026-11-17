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

## Merge-forward r3 Kickoff

- Goal and user-visible outcome: merge origin/main including #524 into #554 while preserving the accepted LFP/DII production metadata behavior and source-withdrawal correction. No new product logic is authorized.
- Expected files: main's merge contents and PR.md only for conflict resolution and validation evidence; preserve this branch's PR.md.
- Acceptance check: a normal two-parent merge of b43f4c8da909ffabe2a4072ae284ca0bac0143b0 and fetched origin/main; only PR.md may conflict; unchanged PR-only three-dot diff; private-terms scan exits 0; full local parity green with zero unexpected omissions or NEAR TIMEOUT, retaining 108 s general, 125 s lifecycle-resume and 135 s row-budget ceilings.
- Issue: https://github.com/Exculpata/exculpata/issues/503; PR: https://github.com/Exculpata/exculpata/pull/554; merged dependency: https://github.com/Exculpata/exculpata/pull/524.

Recorded before the merge. The fetched issue is unassigned, fetched issue comments are empty, and PR comments confirm r2 passed at the prescribed baseline. The r3 scope matches the supplied merge-forward authorization; later slices remain excluded. No remote publication is authorized.

## Merge-forward r3 record

Merged origin/main including #524 (af3ee519): `af3ee519c6c0797b0e1b9f97e5bbe4de7de04522`. This is also the current fetched main SHA. The first parent is `b43f4c8da909ffabe2a4072ae284ca0bac0143b0`. Only PR.md conflicted; resolution preserves the branch record above. Main's speedup files are accepted unchanged. No product, test, workflow or script conflict occurred and no new logic was introduced.

Validated merge SHA: `b3c503ba415294474c1a652df32d5dc37ad0855a`. Both parents are recorded above. The final local amendment changes this validation record only and retains those exact parents; all other blobs remain identical to the tested merge.

### Validation

- Three-dot diff against origin/main: the same 17 PR paths as r2. Every PR code/test blob equals r2; every incoming #524 blob outside PR.md equals origin/main. No speedup file appears in the PR-only diff.
- Required three focused files: **101 passed**, 68.41 s. Original changed-bytes probe: **20 independent instances passed**, 10.64 s with four workers; the unchanged r2 helper creates fixtures, not retries.
- Full local parity recheck: **45 ran, 0 failed, 30 expected NOT RUN, 0 not_run_unexpected; GREEN OVERALL**. No NEAR TIMEOUT entries. Application shards: **9,684 and 14,704 passed**; fast corpus: **526 passed**; transcription: **330 passed**.
- The unchanged 30% gate passed. Highest reported application phase total: **102.75 s / 108 s**. Serial post-shard totals: lifecycle resume **94.56 s / 125 s**; row_budget[10000] **86.89 s / 135 s**. Both serial nodes ran together with `-n 0`, after both shards, using the unmodified workflow.
- No new skip, xfail, assertion weakening, timeout change, scanner/allowlist change, or product logic. Existing service-dependent omissions and inherited xfails remain explicit in the reports.

The first full run is retained as failed evidence: 43 ran, 4 failed, 29 expected NOT RUN, 0 unexpected. One network-checker subprocess hit its existing 45 s timeout. Four reported phase totals exceeded 108 s: operational-name registration 154.63 s, documentation rename contract 139.84 s, wide count-only memory control 121.79 s, and shard collection 116.04 s. Dependent serial tests did not run in that failed attempt. Another full parity process overlapped it. After that process exited, all five affected tests passed unchanged in isolation (call times 9.18, 73.64, 72.21, 51.56 and 55.21 s respectively), then the identical full parity command passed on the same merge SHA. No failure was retried inside a test and no code was changed to obtain the pass.

Evidence is retained in `r3-evidence/` beside the worktree: `revisions.json`, focused and repetition logs, `parity.txt` and its logs for the first run, the timing diagnostic command/log, and `recheck/parity.txt` with its logs for the passing full run. `recheck-result.json` summarizes the passing evidence. The earlier r2 evidence above remains historical.

### R3 disposition

Merge-forward and local validation are complete. Only the validation record is amended after the tested merge; the launcher will validate the final record-bearing commit. The added-lines private-terms scan and diff checks pass before committing. `last.md` is excluded and untracked. Nothing was pushed; no other branch, launcher, configuration, scanner or allowlist was edited.
