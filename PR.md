Design note: Plain natural-language queries use an OR of PostgreSQL-parsed terms and rank partial matches with `ts_rank_cd`. Quoted phrases and explicit negation retain `websearch_to_tsquery` semantics.

## Changes

- The product behavior and synthetic regressions requested by issue #281 are already present in the stacked checkout at commit `63c5a867`.
- Recorded the design and validation evidence without changing the diagnostics files owned by the base branch.
- A higher-level product fix was not appropriate because the requested behavior is already implemented at the PostgreSQL query boundary.

## Tests

- `python -m pytest -q tests/test_fixture_write_policy.py`
  - `512 passed, 1 warning in 43.59s`
- `PYTHONPATH=src python -m pytest -q tests/test_review_bench_v2_postgres.py tests/test_defense_eval_postgres.py`
  - `17 skipped, 1 warning in 1.97s` because `CASE_REVIEW_POSTGRES_TEST_DSN` is not configured in this environment.
- `PYTHONPATH=src python -m case_intelligence.defense_eval --mode stub --retriever postgres --output-dir <temporary-directory>/head`
  - `PostgreSQL retrieval needs CASE_REVIEW_POSTGRES_TEST_DSN`

## Measured numbers

No measured numbers are reported. The requested before and after PostgreSQL defense-eval runs and `--compare` could not run because this environment has no PostgreSQL service, Docker client, or configured PostgreSQL test DSN.

Merges after the #285 PR; then retarget to main.
