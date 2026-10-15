# Validation record

This record describes synthetic local macOS validation. Required Linux CI results are linked from the PR; local failures below are not represented as green gates.

- `python scripts/contrast.py`: **6 palettes, 222 checks, 0 failures**.
- Appearance/persistence/legacy backup-clean-restore/rollback/export-privacy tests: **9 passed**.
- `tests/test_matter_archive.py`: **44 passed** after recognizing the accent column in the strict schema coverage; global preferences remain excluded from exports.
- Final focused appearance, contrast, media-style, API snapshot and source-size suite: **36 passed**.
- Browser harness structural suite: **185 passed**, 3 sandbox `ps` permission failures. Actual Chrome evidence and mobile/session checks are described in [README](README.md). This is not a claim that every pinned Selenium journey ran locally.
- `make compile` and `make compose-check`: passed. The read-only deployment preflight also passed.
- `rg -i '#ff6d52|#0b1425|#78b9ef' src/case_intelligence/static/`: **0 matches**, exit 1 as expected for no matches. Dusk selectors: **0 matches**.
- `git diff --check`: passed.

Full local `make test-fast` with four workers and storage reserve set to zero: **5,929 passed, 139 failed, 73 errors, 44 skipped**. One failure was the obsolete Dusk-specific CSS assertion; it was corrected to assert shared token styling and passes in the final focused suite. The other 211 failing/error test nodes also fail on an unchanged `dbd3c7f` snapshot on this host. That snapshot has additional failures and is not a clean baseline. Known platform gaps include hardcoded Linux media binary paths; no media/security checks were removed or weakened.

`make test-transcription` with loopback access has one remaining failure in the oversized Nemotron subprocess-output test, also reproduced on the unchanged snapshot. The sandbox-only run additionally denied loopback binds. Local `make secret-check` cannot run because Gitleaks 8.30.1 is unavailable. Local `make test-postgres` cannot run because a disposable PostgreSQL 17 + pgvector test service/DSN is unavailable.

`make check-fast` stops at the checkout-wide publication scan. That full scan reports historical metadata/hostname rules on other reachable history, without printing matched values. The exact outgoing-history candidate check passed for the implementation commit; it and the installed external pre-push guard must pass for every published head. No check, hook, threshold, or publication safeguard was bypassed.
