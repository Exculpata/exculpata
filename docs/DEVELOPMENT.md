# Development setup and validation

See [Contributing](../CONTRIBUTING.md) for the short path and
[review policy](QUALITY_GATES.md#review-policy) for merge requirements.

## Set up a development checkout

The complete development gate is exercised on Ubuntu 24.04 with **Python 3.12**.
The application requires 3.12 or newer, but the bundled transcription package
requires a version below 3.13, so their shared environment currently needs 3.12.
The contributor tests do not require a GPU or downloaded model weights.
For code-only work, keep using `make bootstrap` and `make check-fast` below.
To use the product for UI work, follow the separate
[Ubuntu CPU node path](INSTALL.md#cpu-node-quick-start-ubuntu-2404), then
[sign in and create a synthetic practice matter](FIRST_RUN.md#cpu-practice-matter).

Clone your fork and enter the repository. Before committing, configure Git
with your public GitHub username and the no-reply address shown in your GitHub
email settings (`git config user.name` and `git config user.email`). Ordinary
personal addresses are rejected by the publication check unless explicitly
reviewed by a maintainer. Use your own identity, not a maintainer's: contributors
retain credit for their work when a PR lands. The maintainer's identity applies
only to commits created by the maintainer or on their behalf. All contributors
follow the same publication and PR review requirements.

Then install the host tools. On a fresh
Ubuntu 24.04 development machine:

```console
sudo apt-get update
sudo apt-get install --yes git make python3.12 python3.12-venv \
  ffmpeg imagemagick poppler-utils tesseract-ocr tesseract-ocr-eng tesseract-ocr-spa
```

Install Docker Engine with the Compose v2 plugin using Docker's instructions
for your distribution. Verify `docker compose version` before running the full
gate. These are development-machine prerequisites; the bootstrap never installs
system packages, starts services, or downloads model weights.

From the repository root:

```console
make bootstrap
make check-fast
```

Bootstrap creates `.venv`, installs the application and bundled transcription
service together in editable mode (including test, PostgreSQL, and browser
client dependencies), then runs `pip check` and installs the external pre-push publication hook. Existing
external hooks are preserved; branch-owned hooks are rejected. Package installation requires access
to a Python package index. A browser and matching driver must be available
separately for browser acceptance; installing Selenium alone does not install
Chrome. Optional integration tests can skip when their prerequisites are absent.
`make check-fast` runs the local PR gates described in [Quality gates](QUALITY_GATES.md#local-validation).
`make check` runs compilation, Compose validation, publication inspection,
application tests, and transcription tests. Bare `make` still runs application
tests; it does not install anything.

It is safe to rerun bootstrap with the same interpreter after dependency metadata
changes. An existing incompatible or broken environment is left untouched:
choose a new `--venv` directory. The script never deletes an environment or
silently replaces its interpreter. If a download or install fails, correct the
reported problem and rerun the same command; readiness is printed only after
all installation and dependency checks succeed.

`SYSTEM_PYTHON` chooses the interpreter for `make bootstrap`:

```console
make bootstrap SYSTEM_PYTHON=/usr/bin/python3.12
```

Alternatively, the script can be launched by an older Python while selecting
Python 3.12 explicitly. Custom environment paths, including absolute paths and
paths containing spaces, work with all Make test targets:

```console
python3 scripts/bootstrap-dev.py --python python3.12 --venv "/tmp/recordbench dev"
make check PYTHON="/tmp/recordbench dev/bin/python"
```

Use `--dry-run` to inspect the selected/existing interpreters and preview commands
without creating or modifying an environment or installing packages:

```console
python3 scripts/bootstrap-dev.py --python python3.12 --dry-run
```

The printed commands are for the repository root. Windows contributors should
use a Linux development environment such as WSL2 for the complete gate; native
Windows setup and the full native Windows suite have not been validated.
The bootstrap's Windows interpreter-path support alone is not that validation.

GitHub Actions installs and reruns the same bootstrap, then runs application,
transcription, sanitizer, compilation,
standard Compose, Kerberos overlay, and non-interactive installer contracts. The
workflow actions are pinned to reviewed commit SHAs.

## Development model

Use the [product north star](PRODUCT_NORTH_STAR.md) to frame the user outcome
and [GitHub issues](https://github.com/Exculpata/exculpata/issues) to select active work.
The [core evolution plan](CORE_EVOLUTION.md) guides incremental service and
repository extraction. A new feature should strengthen those boundaries while
preserving atomic authorization, source validation, and saving. Update linked
capability claims when behavior changes; keep historical receipts dated.

Tests that start background workers must respect their job ownership and the
workspace transaction lock. Prefer public workflow transitions over direct SQL
to establish retry states. If a fixture manually claims and completes a job,
stop and join that job's coordinator before queueing it; retain live workers in
tests that exercise automatic processing. A passing rerun alone does not fix a
fixture race. Direct SQL fixture writes must acquire `workspace._lock` before
entering the connection transaction; stopping one coordinator does not stop the
other workers that share that connection.

The frozen Review acceptance pack pins test nodes and their complete files,
including helpers. Changes to those files require reviewed node/file digests
and an updated aggregate content fingerprint, even when case selection and
expected outcomes stay the same. Run `tests/test_review_acceptance_pack.py`
alongside the affected workflow tests when updating these fixtures.

Exculpata OSS is the upstream source for portable product behavior. Work from
a focused branch or worktree, keep one reviewable concern per change, and merge
only after its synthetic regression and relevant operator documentation pass.
Do not develop reusable behavior only inside a private deployment and copy its
history back later.

If an issue is discovered in a downstream deployment:

1. describe the behavior without private source material, logs, paths,
   identities, hostnames, or configuration;
2. reproduce it here with an unmistakably synthetic fixture;
3. implement and validate the general correction in this repository; and
4. let the downstream record and consume the resulting reviewed revision.

Downstream-specific identity mappings, certificates, storage coordinates,
capacity policy, secrets, and service wiring belong in an external deployment
overlay. They are not acceptable additions to this repository even when the
downstream is private.

## Standalone browser journeys

On merge groups, schedules and manual dispatch, the quality workflow also runs the synthetic intake and Report/export-readiness
journeys with a checksum-pinned browser and driver. See
[browser acceptance](BROWSER_ACCEPTANCE.md) for local commands, bounded
artifacts, deliberate browser updates and rollback. These journeys use disposable
loopback applications and require no model downloads or deployment access.

## PostgreSQL integration CI

The `postgres-integration` Quality gates job starts a disposable, loopback-only
PostgreSQL 17/pgvector service from an immutable image digest and installs the
existing PostgreSQL test dependencies. It runs the seven existing database
acceptance tests for migration/reopen, page citations, media projection,
matter isolation, deletion and rebuild. The report check fails on missing,
empty, incomplete, skipped or unsuccessful results. Additional database tests
in the same module are included automatically.

To reproduce against an isolated disposable database, set
`CASE_REVIEW_POSTGRES_TEST_DSN`, install `.[dev,postgres]`, then run:

```console
python -m pytest -q tests/test_review_bench_v2_postgres.py \
  --deselect tests/test_review_bench_v2_postgres.py::test_live_learned_dense_only_paraphrase_and_unsupported_abstention \
  --junitxml=/tmp/recordbench-postgres-results.xml
python scripts/check-postgres-test-report.py /tmp/recordbench-postgres-results.xml
```

The tests mutate the database; use synthetic disposable data only. The service
and data are discarded with the hosted job. No deployment credentials, models,
or external model worker are used. Learned-model acceptance is explicitly
excluded and remains separate. This job exercises the existing hybrid backend;
it does not add a PostgreSQL exact-search adapter or complete the shared-corpus
PostgreSQL acceptance required by slices 00/08.
