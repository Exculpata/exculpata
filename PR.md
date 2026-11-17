# PR #524: deterministic reports browser timezone

## Kickoff

- Goal: preserve the local parity concurrency and serial lifecycle work while making the reports journey deterministically display America/New_York viewer time under the UTC CI runner. Users retain the UTC fallback and local-time display assertions.
- Expected files: scripts/browser-accept-reports-bundle.py, tests/test_browser_acceptance_runner.py, docs/QUALITY_GATES.md, and this PR.md. The runner/environment helpers are in scope only if diagnosis requires them.
- Acceptance: a non-browser application regression fails on starting commit bd45657d396824cd795ae3e25605a3d21abeb0ac and passes after the launch fix; ten reports journeys with TZ=UTC pass using local Chrome; existing UI assertions, serial lifecycle/row-budget placement, 125 s/135 s ceilings and overlap regression remain unchanged. Run focused checks and publication scanning, then commit without pushing.
- Issue/work item: https://github.com/Exculpata/exculpata/pull/524 and the supplied 2026-11-16 r2 CI-only miss request. No separate issue is linked by the PR. Current PR metadata/comments show no assignees or conflicting hold; the draft remains unmerged.

This kickoff was recorded before implementation and is included in the first fix-up commit. Scope matches the supplied r2 acceptance criteria.

## Root cause and fix

CI-ONLY MISS: local parity passed because it does not provision browsers and the synthetic-browser journey step is NOT RUN locally; GitHub ran the reports journey with TZ=UTC, and its one-time target-scoped CDP timezone override did not cover the check-in page, which rendered viewer time as UTC instead of America/New_York. The harness had no process-level timezone guarantee.

The failure HTML in [run 43947944687, artifact 14266361345](https://github.com/Exculpata/exculpata/actions/runs/43947944687/artifacts/14266361345) contains `Your time: Feb 21, 2027, 8:18:55 PM UTC (UTC)`. The permanent UTC timestamp and viewer-local text both rendered, so the template and lifecycle JavaScript behaved consistently with the wrong browser environment. The preceding head passed on the same pinned browser, and the parent delta did not change the browser or product rendering code.

A local real-browser scope probe under TZ=UTC returned UTC initially, America/New_York after the original CDP command, then UTC in a new target. With the process launch fix and no CDP command, both initial and new targets returned America/New_York while the Python process retained UTC. This establishes the missing process-level guarantee. The exact event that invalidated the override in the hosted run was not logged; renderer replacement/download timing remains an inference, not a claimed reproduction. One instrumented baseline reports journey on local Chromium 155 passed naturally, consistent with an intermittent failure.

The reports journey now passes `TZ=America/New_York` directly to ChromeDriver's Service environment, inherited by Chrome and renderer descendants. It retains the existing CDP override and checks the actual Intl zone immediately before the existing UTC/viewer-time assertions. The CDP call remains to preserve the existing network-contract inventory; the fix does not depend on it surviving. No scanner or allowance was edited. All other journey/application environments retain their existing behavior. No product code, retry, timeout, wait, skip or xfail was changed.

Fix level: the launch boundary owns the timezone; the application regression checks the real journey entry point and real Service object before substituting browser launch. No lower-level policy exception is needed.

## Local parity coverage

`test_reports_browser_timezone_survives_loss_of_cdp_session` is in the existing browser-runner test module, selected by application shard 0. It executes `main()` through the browser launch boundary with the runner's actual child environment and with a standalone UTC environment, asserts the Service receives America/New_York, and verifies the parent remains UTC. The application/server/socket/thread and browser process are substituted; no browser installation is needed. Removing the launch environment override makes both cases fail despite the retained CDP call.

Local parity still explicitly reports browser provisioning and actual synthetic journeys as NOT RUN. The ten local browser executions are separate acceptance evidence, not a claim that the parity tool now provisions or runs Chrome. The launcher will run full parity after this fix-up commit. No new hosted CI pass or review is claimed.

## Preserved scope

The lifecycle resume and row_budget[10000] nodes remain in the serial post-shard step with 125 s and 135 s ceilings unchanged. The deterministic overlap regression from r1, workflow triggers and all other journeys are unchanged. This fix-up does not depend on another issue. No push or merge is performed; merge still requires the maintainer's exact-SHA authorization.

## Validation

- Regression baseline: the new two-case non-browser test was run before the launch fix against the unchanged reports code from bd45657d396824cd795ae3e25605a3d21abeb0ac. Both cases failed specifically on `UTC != America/New_York`. Both pass after the fix.
- Final focused suite: `python -m pytest -q tests/test_browser_acceptance_runner.py`: 278 passed.
- Fixture/documentation contracts: `tests/test_fixture_write_policy.py tests/test_fixture_inventory.py tests/test_fixture_inventory_coverage.py tests/test_fixture_reflective_deny.py tests/test_documentation.py`: 2057 passed.
- Preserved scheduling checks: `test_serial_tests_are_in_local_plan_after_shards` and `test_serial_tests_wait_for_shards_and_run_alone`: 5 passed. The r1 `test_independent_cells_overlap_with_bounded_workers` regression also passed all 4 cases. The workflow, parity implementation, limits and r1 overlap test have no diff from the starting commit.
- Ruff on the changed Python files and the unmodified network-contract scanner passed. Initial removal of the CDP command caused a stale inventory entry; retaining the existing command resolved that without changing the scanner or its allowances.
- Browser acceptance: 10/10 consecutive final-form reports journeys passed with parent and runner `TZ=UTC`, using local Chromium/ChromeDriver 155.0.8059.84 (hosted CI used pinned 153.0.8010.36). Each run used the existing runner child environment, original reports row with `--verify-readiness`, a fresh short temporary workspace/browser, the unchanged 300 s process budget, and the original 16-check receipt validation. All ten receipts contain 16 checks; no navigation-timeout diagnostic was produced. Durations in seconds: 74.22, 83.86, 84.91, 84.81, 78.42, 80.55, 79.29, 82.82, 74.97, 71.7.
- An earlier local driver invocation used a checkout-length temporary path and Chrome exited before starting the journey. The final ten runs use the runner-style short temporary path. This changed only the disposable validation driver, not repository timeouts or acceptance behavior.
- Publication safeguards: `private-terms-scan --added-lines` exited 0; `publication-check.py --skip-history` reported CLEAN after generated diagnostics were moved outside the source tree. `git diff --check` and the PR-body checker passed. Git history publication scanning and full CI parity remain the launcher's checks; no push was performed.
