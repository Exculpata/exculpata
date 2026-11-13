# Kickoff

- Goal: report GREEN OVERALL, including SUMMARY RESULT, when executed checks
  pass and every NOT RUN cell is an expected skip. Preserve failure and unlisted
  skip results and exit codes so reviewers can distinguish accepted gaps from
  failed or unexpectedly unavailable checks. Scoped-run wording stays unchanged.
- Expected files: scripts/ci_parity.py, tests/test_ci_parity.py, and the Local
  parity sentence in docs/QUALITY_GATES.md, plus this kickoff record.
- Acceptance check: synthetic CLI regressions prove listed skips are GREEN
  OVERALL with exit 0, failures are NOT GREEN with exit 1, and unlisted skips are
  NOT GREEN with exit 3. Run focused pytest, Ruff, full parity on the candidate
  tree, and the required added-lines publication scan. Preserve the expected-skip
  list, launcher, workflows, configuration, snapshots, and other pilots' files.
- Issue link: no issue URL supplied; the user request “CI parity green expected
  skips” is the scope and acceptance authority. No network lookup is permitted.

The requested scope and acceptance checks match this kickoff. This fixes report
wording at its source; no launcher exception or skip-list change is needed.
Regression baseline: 9eb35b9970ad768f6e5c5889536b7bb2f99e8360. The listed-skip
GREEN assertion fails on that baseline. The reported earlier review failure was
at 74d398b7.
