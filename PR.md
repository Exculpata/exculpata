# PR #554: withdraw changed production sources

## Kickoff

- Goal and user-visible outcome: merge main into the prescribed PR baseline and ensure changed load-file sources stop contributing current associations and completed production counts.
- Expected files: main merge contents; production/service.py (repository.py if necessary), tests/test_discovery_review_image_load_files.py, and PR.md.
- Acceptance check: preserve r1's seven regression cases; reproduce failure before the condition fix; pass the three required files and 20 changed-bytes repetitions with four workers; run local parity on the committed merge; retain the 30% duration rule and 125 s / 135 s ceilings.
- Issue: https://github.com/Exculpata/exculpata/issues/503; fix-up: https://github.com/Exculpata/exculpata/pull/554.

The supplied r2 scope matches this goal. Main and the remote PR baseline were fetched and verified; issue #503 is unassigned with no fetched comments, and #554 is draft with the previous job receipt. Later slices remain excluded. Kickoff recorded before the new product correction; r1's existing regression tests are retained.

## Correction and validation

CI-ONLY MISS: branch-only parity missed main's retained catalog row; merged reconciliation must also reject source_state != 'ready'.

Fix level: product invariant, using the existing stale-attempt cleanup path.
Regression baseline: failed hosted merge 20466b8e72f5eccc2c3205935c2880af4fe2778e, incorporating main 718469390d50c49465fd7e7f64d6661c4ed91363.

Merged-tree regression: all seven cases failed before the condition fix (15.47 s), then all seven passed (15.90 s). Main's pending/restart behavior is retained. Required-file, repetition and full parity checks will run on the committed merge. The earlier scope and parent-contract blockers are resolved; local validation and review remain. No push or merge to main is authorized.
