# Local CI parity replays the PostgreSQL job's environment

Status: design note, r1. Checked against VISION.md and AGENTS.md (see the last section).
Type: test and tooling change only. No product code, no workflow file, no storage guard.

## Kickoff
- Goal in my own words: a pull request must not be able to turn the hosted `postgres-integration` job red for a reason local parity could have seen. Local parity should run the part of that job that needs no Postgres service, under the same environment the hosted job gives it, and say exactly what it could not run.
- User-visible outcome: a fixture that only works under the `application-shards` environment fails the local parity report with a named test and fixture. The report lists each part of the PostgreSQL step it did not replay and why.
- Expected files: `scripts/ci_parity_runner.py`, `scripts/ci_parity_plan.py`, new `scripts/ci_parity_service_replay.py`, `scripts/ci-parity-expected-skips.toml`, `tests/test_ci_parity.py`, new `tests/test_ci_parity_service_replay.py`, `tests/snapshots/ci_parity_plan.json`, `docs/QUALITY_GATES.md`, `docs/NETWORK_EGRESS.md`.
- Acceptance check: the numbered list below.
- Issue link: none. Failing commit for the regression: `e13f991ace5055074f83a3b11d7d1185966a8819` on PR #518.

## Problem
PR #518 passed local parity (`GREEN OVERALL`) at `e13f991a` and then failed the hosted `postgres-integration` job. The new test `test_portable_migration_and_queries_match_sqlite` errored while setting up the shared `production` fixture: the app returned 507 from its managed-storage free-space guard, because that job leaves `CASE_INTELLIGENCE_STORAGE_RESERVE_GIB` unset (default reserve 100 GiB) and a hosted runner has less than 100 GiB free. `application-shards` sets the reserve to 0, so the same fixture had passed there. The fix-up (`a22f40b4`) made the fixture set the reserve itself.

## Root cause
1. Local parity lists the step "Run existing PostgreSQL and pgvector acceptance" as `EXPECTED NOT RUN category=service` and prints one blanket reason. Nothing in that step runs locally, including the parts that never needed a database.
2. The step's tests are marked `postgres` and the `application-shards` job runs `-m "not postgres"`, so no local step ever executes their fixtures.
3. Simply running the step with no DSN would not have caught it. `postgres_base_dsn` (tests/conftest.py) is session-scoped and skips when the DSN is unset. pytest sets up higher-scoped fixtures first, so the skip happens before the function-scoped `production` fixture is touched. Measured, see Dry run: the test is skipped and `production` is never set up.
4. The hosted job's environment (no reserve override) cannot be reproduced by a developer machine with a large disk, where the 100 GiB default passes. The check needs a deterministic hostile value, not "whatever this machine has".

## Approach
Add one reviewed, data-driven replay for expected-skip steps of category `service` whose command is pytest. It is not a new mode and it is not Postgres-specific code in the runner.

1. Data. `scripts/ci-parity-expected-skips.toml` accepts one new optional key, `replay`, with the single allowed value `pytest-service-stub`. It is allowed only on `category = "service"` entries. Any other value, or the key on another category, fails the plan with `UNMAPPED` (exit 5). Only the PostgreSQL step gets the key. Other `service` steps (encrypted restore drill, defense-eval) keep the blanket NOT RUN and are listed as follow-up.
2. Extraction. The replay takes the step's own `run` text, joins backslash continuations, skips heredoc bodies, and keeps each command line whose first tokens are `python -m pytest` (or `python3 -m pytest`). It never keeps a hand-copied command, so a test file added to the workflow step is replayed with no second edit. No such line means `UNMAPPED` (exit 5). Every other command in the step is printed as `REPLAY NOT REPLAYED` with the first 80 characters, and is never run.
3. Environment, built from the same layers the runner already renders (workflow env, job env, step env):
   - `CASE_REVIEW_POSTGRES_TEST_DSN` is forced to the fixed sentinel `ci-parity-replay-stub-dsn`, overriding the job's literal DSN and any inherited value. No replay can reach a real server.
   - Step env entries that use `job.services` are not rendered and are printed as omitted (the Docker client prefix).
   - `CASE_INTELLIGENCE_STORAGE_RESERVE_GIB`: if no layer declares it, the replay sets `16384`, the largest value the policy accepts, so the guard trips on any machine. If a layer declares it, that value is used unchanged, so a step that declares `0` replays honestly.
   - Inherited `CASE_INTELLIGENCE_TEST_MODE` is removed unless a layer declares it. The suite's `conftest.py` then sets it exactly as the hosted job does.
4. Plugin. A new module `scripts/ci_parity_service_replay.py`, loaded from the parity tool's own directory through `PYTEST_PLUGINS` and a prepended `PYTHONPATH` entry (never from the tree under test), does three things:
   - Supplies the sentinel for the two session fixtures `postgres_base_dsn` and `postgres_test_dsn` so setup continues past them.
   - Skips running the body of any test whose fixture closure includes a stubbed fixture. Every other fixture of that test is still set up.
   - Records each outcome and writes a small JSON report to a path the runner passes in the environment.
   Tests whose closure has no stubbed fixture run normally under the hostile environment.
5. Classification. A failure is UNREPLAYABLE only when its exception chain contains an exception from the `psycopg` package or mentions the sentinel, or is a missing `psycopg` module (also at collection). Everything else is FAIL, including every setup error from a non-database fixture.
6. Report lines, added before the unchanged final `SUMMARY` line:
   - `REPLAY <workflow:job:step> reserve=<value> (hostile|declared) dsn=stub`
   - `REPLAY NOT REPLAYED <command>`
   - `REPLAY UNREPLAYABLE test=<id> phase=<setup|call|collect> reason=<exception type>`
   - `REPLAY FAIL test=<id> phase=<setup|call|collect> <exception type>: <first line, 120 characters at most>`
   - `REPLAY RESULT stubbed_setup_ok=N body_skipped=N ran=N passed=N skipped=N unreplayable=N failed=N`
7. Outcome. The existing `NOT RUN (expected-skip)` line stays, because the step is still not fully run. Any REPLAY FAIL, an unclassified pytest exit code (not 0 or 1; 5 means nothing was collected), or exit 1 with no recorded failure or unreplayable entry counts as one failed step, so the existing path gives exit 1 and `NOT GREEN, FAILURES`. The `SUMMARY` format and every existing exit code are unchanged.

Known limit, reported not hidden: when a test's first Postgres-needing fixture is not one of the two stubs, fixtures set up after it are not replayed. The `production` fixture in #518's test is requested first, so it is covered.

Options not taken:
- Replay with no DSN and no stub: skipped before the fixture runs, so it would not have caught #518 (root cause 3).
- Force the reserve to 0 for every test in `tests/conftest.py`: hides the class, and `tests/test_managed_storage.py` checks the default.
- Install Postgres locally: needs an install approval and is not required for this class. Still the way to run the test bodies.
- A parser of the workflow file outside this repository: the parity logic lives here, so the fix lives here.

## Blast radius
- Hosted CI: none. No workflow file changes. The `postgres-integration` step is unchanged.
- Local parity time: one extra pytest run over the PostgreSQL step's test files with database tests set up but not run. Expected to be minutes, measured in the job.
- A false red on an unrelated pull request is the main risk: an existing database-marked test whose setup fails for a reason that is not a `psycopg` error. Acceptance 11 measures this on main before the change can merge.
- `tests/snapshots/ci_parity_plan.json` changes (the `skips` entry). Any other open pull request that edits the workflow or the skip data will need the snapshot regenerated after this merges.
- `docs/NETWORK_EGRESS.md` lists every file-name literal in parity scripts by digest. New literals in `scripts/ci_parity*.py` need rows there, written by the process the table already describes, not by suppressing the check.
- Adding a test file shifts which file lands in which application shard. Shard membership is by sorted position; no test is dropped (the drift test counts each file once).
- Nothing here touches product behavior, the storage guard, egress, privacy or evidence integrity.

## Proving test
`tests/test_ci_parity_service_replay.py` builds a synthetic repository in a temp directory with a workflow whose service step runs pytest, a `conftest.py` with a session-scoped fixture that skips when the DSN is unset, a function-scoped `production`-style fixture that fails when the reserve is not 0, and a test requesting `production` then a connection fixture. It runs the real `scripts/ci_parity.py`.
- Without the `replay` key (today's behavior): exit 0, `GREEN OVERALL`. This control proves the blind spot.
- With the key: exit 1, a `REPLAY FAIL` line naming the test and `phase=setup`.
- After the fixture sets the reserve to 0 itself: exit 0 and `REPLAY RESULT` with `failed=0`.
This is the #518 sequence (`e13f991a` red, `a22f40b4` green) on synthetic data. The same sequence is also run on the real heads of #518 (acceptance 12).

## Acceptance
1. With the `replay` key and a fixture that fails unless `CASE_INTELLIGENCE_STORAGE_RESERVE_GIB` is 0, the synthetic run exits 1 and the report contains exactly one `REPLAY FAIL` line naming the test with `phase=setup`. The same repository without the key exits 0 with `GREEN OVERALL` (control).
2. After the fixture sets the reserve to 0 itself, the same run exits 0 and `REPLAY RESULT` shows `failed=0`.
3. When no layer declares the reserve, the replay sets 16384 and the report line says `hostile`. When the step env declares `0`, the value is used unchanged, the report says `declared`, and the first-failure fixture passes.
4. With the parent environment and the job env both holding a real-looking DSN, a synthetic fixture observes exactly `ci-parity-replay-stub-dsn`. The job's literal DSN is never present in the replay environment.
5. A test whose closure includes a stubbed fixture has its body skipped and counts under `body_skipped`; its other fixtures are set up (exactly one synthetic marker file written by a fixture exists after the run). A test with no stubbed fixture runs and counts under `ran`.
6. An exception from a synthetic `psycopg` stand-in raised in setup yields `REPLAY UNREPLAYABLE` with the exception type and exit 0. A `RuntimeError` raised in the same position yields `REPLAY FAIL` and exit 1. A missing `psycopg` module at collection is unreplayable; any other collection error is a failure.
7. A replay-marked step with no pytest line exits 5 with `UNMAPPED`. A line that begins `python -m pytest` inside a heredoc body is not run. The non-pytest commands of the real step are each printed as `REPLAY NOT REPLAYED` and none is run.
8. A pytest run that collects zero tests, or ends with exit 3 or 4, is a failed step, exit 1.
9. The `replay` key on a non-`service` entry, or with any value other than `pytest-service-stub`, exits 5. The key appears on exactly one entry of the real skip file.
10. The final report line still matches the `SUMMARY ran=… failed=… not_run=… not_run_unexpected=… RESULT:` format, the existing `tests/test_ci_parity.py` tests pass unchanged except the regenerated golden plan, and no existing exit code changes meaning.
11. On current main, the replay of the real PostgreSQL step reports `failed=0`. Every `unreplayable` entry is listed by test id in the job's `last.md`. At least one test in `tests/test_defense_eval_postgres.py` or `tests/test_review_bench_v2_postgres.py` shows `body_skipped`.
12. In a throwaway local worktree (never pushed) at #518's head `e13f991a` with this change's `scripts/` and skip file copied in, the real replay reports a `REPLAY FAIL` for `test_portable_migration_and_queries_match_sqlite` with `phase=setup`; at `a22f40b4` with the same copy, it reports `failed=0`. If a head is not reachable, `last.md` says so and acceptance 1 and 2 stand alone.
13. `ruff check scripts tests`, `make network-contract`, `tests/test_network_contract_filenames.py` and the private-terms scan all exit 0, with new literals recorded in `docs/NETWORK_EGRESS.md` and none suppressed.
14. `docs/QUALITY_GATES.md` "Local parity" describes the replay in at most one paragraph, including the sentinel, the hostile reserve and what is still not run.
15. No file under `.github/` and nothing under `src/` is in the diff. Both are empty in `git diff --name-only origin/main...HEAD`.

## Dry run
Date 2026-11-14, scratch directory, pytest 9.2.0 (repository pin is `pytest>=9.0.3,<10`), a conftest with the same session-scoped skipping fixture and a `production`-style fixture that fails unless the reserve is 0.
- `env -u CASE_REVIEW_POSTGRES_TEST_DSN python -m pytest -q -rs --setup-show test_order.py`: result `1 skipped`, exit 0. `--setup-show` lists only `postgres_base_dsn`; `production` was never set up. (Root cause 3.)
- Same command with a 15-line plugin that stubs the session fixture and `CASE_INTELLIGENCE_STORAGE_RESERVE_GIB=16384`: result `1 error`, exit 1, at setup of `production`, the failure #518 had.
- Same plugin with the reserve set to 0: result `1 passed`, exit 0, body not run.
The runner, plugin, and report were not built in this dry run; that is the implementation item.

## Fence
Plain grep of every file that references the parity tool, its skip data, or the database fixtures this change stubs. Each file is listed with what happens to it.

```
git grep -l -I -E -e 'ci_parity|ci-parity' -- .
```
- `scripts/ci_parity.py`: entry point; prints report lines and the final `SUMMARY`; unchanged except as needed to print `REPLAY` lines.
- `scripts/ci_parity_runner.py`: edited. The expected-skip branch gains the replay call.
- `scripts/ci_parity_plan.py`: edited. `load_skips` accepts the optional `replay` key under the rules in Approach 1.
- `scripts/ci_parity_environment.py`: read only. Its `ENVIRONMENT['remove']` list already drops the inherited DSN.
- `scripts/ci_parity_expr.py`: not edited.
- `scripts/ci_parity_service_replay.py`: new.
- `tests/test_ci_parity.py`: edited only to add the replay key to its skip data if a test needs it; the golden plan test is regenerated, not loosened.
- `docs/QUALITY_GATES.md`: edited (acceptance 14).
- `docs/NETWORK_EGRESS.md`: edited (acceptance 13).
- `tasks/ci-parity-green-expected-skips.md`: historical task file, not edited.
- `tests/snapshots/ci_parity_plan.json`: regenerated with `CI_PARITY_UPDATE_SNAPSHOT=1 python -m pytest -q tests/test_ci_parity.py::test_golden_plan`.
- `scripts/ci-parity-expected-skips.toml`: edited (one entry).

```
git grep -l -I -E -e 'load_skips|expected-skips|expected_skips' -- .
```
- `docs/NETWORK_EGRESS.md`, `docs/QUALITY_GATES.md`, `scripts/ci_parity.py`, `scripts/ci_parity_plan.py`, `tests/test_ci_parity.py`: the same files as above; no others.

```
git grep -l -I -E -e 'postgres_base_dsn|postgres_test_dsn|postgres_schema' -- .
```
- `tests/conftest.py`: not edited. Its two session fixtures are the ones stubbed; a drift test pins their names and scope.
- `tests/postgres_schema.py`: not edited.
- `tests/test_defense_eval_postgres.py`, `tests/test_postgres_fresh_schema.py`, `tests/test_review_bench_v2.py`, `tests/test_review_bench_v2_postgres.py`: not edited. They use the stubbed fixtures; acceptance 11 measures them.
- `.github/workflows/quality-gates.yml`: not edited. It defines the replayed step (job `postgres-integration`) and sets the reserve to 0 only for `application-shards`.
- `scripts/check-postgres-test-report.py`: not edited and not replayed; it needs a real run's report.

## Workflow edit needed
No. `.github/` is outside the fence (acceptance 15).

## Checked against VISION.md and AGENTS.md
- VISION "never weaken an egress, privacy, or safety check": a check is added; the storage guard and the sentinel DSN keep every replay off the network.
- VISION "never allowlist a single file name or slip past a guard / never answer a repeat failure with another exception": this is the first failure of its kind. The change narrows an existing blanket exemption and lists the remainder; it adds no exception. The one skip-data key is reviewed data, exact by workflow, job and step.
- VISION "no mode or single-use feature": one generic mechanism keyed by reviewed data, not a postgres flag in the runner.
- VISION "design note first": this note, with problem, root cause, approach, breakage and the proving test.
- AGENTS "fix at the highest level that works": a code-level check that fails the gate. A rule or review would not catch the next fixture that assumes another job's environment. Option C (a conftest default) is lower level and hides the class.
- AGENTS "kickoff, failing test first, name the failing commit, synthetic data only": Kickoff above, test list above, commit `e13f991a` named, all data synthetic.
- Neutral public language: no private names, paths or hostnames appear in this note.
