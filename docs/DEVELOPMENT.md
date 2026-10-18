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
python3 scripts/bootstrap-dev.py --python python3.12 --venv "/tmp/exculpata dev"
make check PYTHON="/tmp/exculpata dev/bin/python"
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
fixture race. Use `tests.fixture_write.fixture_write(workspace)` for direct SQL
fixture writes. Use `fixture_write(workspace, media=True)` across a complete
manual media seed sequence (queue, claim, import, finish). Media mode rejects
entry into an existing transaction or active fixture scope on that connection,
including when a reusable seed helper is called indirectly. The helper acquires the current workspace lock
before entering the connection transaction and keeps it through commit or
rollback. Nested SQL fixtures use a savepoint without committing their caller's
transaction, even when the outer fixture has only read or created a table before
entering the nested fixture. Ownership follows the locked thread, so copied or empty Python contexts cannot
hide an active nested scope; finished handles lose ownership. Public media methods may commit individually; the outer helper
serializes the sequence rather than making those methods one atomic operation.
SQL fixture scopes admit connection operations, not workspace method calls:
public mutators can enter their own connection contexts and commit pending fixture
SQL. Prepare pure timestamp values before entry and perform public workflow
transitions outside the SQL fixture. The inventory rejects workspace method calls
inside SQL scopes, including private methods and saved aliases, rather than guessing
which public methods commit. An unknown method name selected with `getattr` still
retains a known workspace receiver for this rule. Explicit media mode retains its separate non-atomic
seed contract. Deliberate isolated callback probes need an exact-statement reason.
Yielded connections, cursors and saved methods are valid only on the owning
thread during the boundary; expired or transferred handles raise before touching
SQLite. Retained, returned or passed cursor results acquired inside a fixture
must come from its yielded handle. Discarded raw write results and immediate
in-boundary fetches remain supported. Row factories also check ownership when
SQLite delivers a row, but empty or exhausted raw cursors never invoke a factory;
the inventory therefore rejects their retention at acquisition.
Writable connection/cursor attributes delegate to SQLite; `autocommit=True`
is unsupported and rejected before entry or assignment. Legacy transaction control
with `isolation_level=None` is also rejected at entry and on scoped mode changes,
because raw DML in that mode commits before an exception can roll it back.
PEP249 `autocommit=False` remains supported on entry when its ignored isolation
level is `None`: that transaction belongs to the caller. Switching to that mode
inside a fixture is rejected because each commit or rollback immediately opens
a replacement transaction that would outlive the fixture lock.
Custom row factories receive scoped cursors, including through public workspace queries, and scoped
connection factories are always restored before releasing the lock, including raw
in-boundary replacements. Set shared connection and cursor row factories through
their yielded handles, even while holding the workspace lock: a raw callback can expose
the underlying connection beyond that lock. Custom cursor
factories are rejected before they can receive a raw connection. Unsupported
connection methods such as closing the shared database, blob access, deserialize,
backup and deferred dumps fail closed rather than returning an unscoped object.
Calling a shared connection itself, including explicit `__call__` descriptors,
is unsupported: preparing a raw SQLite statement can change PRAGMA state. Use
the yielded handle's ordinary SQL methods instead.
Deleting connection or cursor attributes is unsupported on yielded handles.
The inventory rejects shared row/text factory deletion, database deserialization,
blob handles, backups into a shared destination, `setconfig`, and extension-loading
operations, including SQLite descriptor forms. A raw backup from the shared source to an independent destination must
hold the source workspace lock outside a fixture transaction. Backups inside
`fixture_write` remain unsupported even through the raw connection.
Connection-limit changes and interruption also
require the owning lock or fixture boundary; deliberate cross-thread cancellation
experiments need an exact-statement concurrency reason.
Use the yielded handle for scripts, connection contexts and explicit transaction
control, including direct, saved or unbound `__enter__`/`__exit__` calls.
DDL, transactional metadata PRAGMAs, CTE writes and semicolon/BOM-prefixed writes
require that handle, which begins a transaction before SQLite can execute them
without an implicit transaction. PRAGMA setters support only `user_version` and
`application_id`; other setters and action PRAGMAs fail before execution, including
inside scripts and EXPLAIN. Read-only PRAGMA queries remain supported. Deliberate
independent-connection configuration, such as a contention writer's busy timeout,
requires the owning lock and an exact-statement reason. ATTACH, DETACH and VACUUM
are rejected; deliberate raw attachment injection is limited to explicitly
annotated negative tests on isolated synthetic connections. Outer scripts execute complete SQLite statements within the fixture
transaction, without sqlite3's implicit pre-script commit; explicit transaction
commands and rollback conflict policies in scripts are rejected before execution.
A nested handle rejects `executescript`, commit/rollback, transaction-control SQL, rollback conflict clauses and
transaction-mode changes or connection context entry before they can end the caller's transaction. The same
restriction applies to a retained outer handle or cursor during the nested
boundary. Use ordinary `execute`/`executemany` statements for nested SQL fixtures;
the helper preserves any existing SQLite authorizer.
Nested fixtures reject schemas containing transaction-wide rollback constraints
or triggers before entry and each SQL operation, including newly attached databases.
Do not retain the yielded connection for later writes or wait for workers/HTTP
while holding the helper. Keep source mutation guards outside it.

`tests/test_fixture_write_policy.py` checks test modules in the normal application
CI suite and names `fixture_write` when a write bypasses the boundary. Deliberate
independent-connection contention, caller-owned transaction assertions, and
stopped-database migration/restore tests require a narrowly documented exception
at the specific statement; ordinary fixture setup must use the helper. The separate
`SQLiteStore` adapter deliberately uses legacy autocommit; isolated integrity and
corruption fixtures for that adapter use its public explicit `transaction()`
context with exact-statement reasons, preserving the adapter's native mode.
Generator bodies are deferred work even when created inside the helper. Put
their writes in a function that acquires its own fixture boundary, or use an
eager operation while holding the helper. Function decorators, defaults and
annotations that Python evaluates immediately require the boundary
at definition time; function and lambda bodies remain deferred. Comprehension
loop names have their own scope and do not inherit an independent connection alias.
The inventory follows constant-name getattr/setattr operations, SQLite method
and writable-attribute descriptors called with an explicit receiver, and element-wise tuple/list
unpacking, including nested and starred assignments.
Literal tuple/list iteration preserves element ownership, including nested loop
targets and saved methods. Attribute setters in unpacked assignments, loops and
comprehensions follow the same boundary rules as direct assignments, evaluating
and binding leaves left to right. A cleanup container containing only independent
connections and ordinary resources remains non-shared; any known shared member
retains the shared boundary requirement.
Target expressions are visited before the following assignment leaf, so their
explicit local bindings cannot hide a later shared setter. Subscripting literal
containers with known shared values, destructuring known shared match subjects,
and passing shared values through variadic helper parameters are unsupported by the inventory. Use explicit named
handles and fixed helper parameters for fixture setup; independent-resource
controls and simple match captures remain supported.
Non-destructuring literal and whole-subject match aliases remain supported.
Subscripts capture their base before evaluating the index, and calls capture the
callee before evaluating arguments. Assignment expressions in call arguments
involving known shared ownership are unsupported; bind those values explicitly
before calling. Heterogeneous container merges that lose a known shared shape
and shared global/nonlocal writes from helpers also reject explicitly. Return
named handles from fixed-parameter helpers instead of changing outer bindings.
Comprehension, lambda and class locals retain their separate binding scopes.
Class bodies track their own explicit global/nonlocal declarations. Direct
Boolean values used as iterables, arguments or subscript bases receive the same
unresolved-ownership rejection as assigned containers. Synthesized capture and comprehension targets retain source
locations; propagation after a comprehension does not count as another write.
Call checks inspect starred operands before expansion. Opaque tuple/list or
dictionary expansions and dynamic dictionary keys cannot erase a known shared
member, whether carried by a dictionary key or value: such containers retain
unresolved ownership and reject explicitly.
Dictionary inputs are checked in key/value evaluation order. Shared dictionary
comprehension results and attribute/view access on unresolved shared containers
are unsupported; use explicit named handles. String and path conversions retain
active-database path provenance without retaining a reachable workspace handle.
Literal elements and comprehension results retain their evaluated ownership;
an exception on construction does not exempt later unannotated consumption.
Known shared mappings reject value-returning accessors while literal-key iteration
remains allowed. Only proven scalar converters discard handle ownership; ambiguous
rebindings remain conservative. Active workspace paths still specialize fixed
local helper parameters so indirect database opens cannot lose path provenance.
Accessor results retain unsupported provenance after exact extraction exceptions,
and unbound dictionary descriptors check their explicit receiver. Value-returning
views over known active workspace paths are also unsupported; literal keys remain
non-owning. Branches composed entirely of proven scalar converters stay proven.
It merges match cases conservatively, propagates explicit class global/nonlocal
assignments, and recognizes keyword workspace arguments. Dynamic
PRAGMAs require a yielded handle; use parameterized table-valued PRAGMA queries
for dynamic schema inspection. Class-local aliases remain separate from module and enclosing function aliases;
methods and comprehensions resolve names through their lexical scope. A direct
SQLite connection to a workspace path keeps that provenance: exempting its
creation does not exempt subsequent writes, contexts or transaction control.
Alias assignments across branches, loops and exception paths are merged
conservatively. Dynamic SQL must use the yielded handle so transaction-ending
statements cannot bypass its runtime checks.
Deferred bodies include later global and closure bindings while preserving
definition-time defaults. Unannotated helper parameters that mutate SQLite
callbacks must establish ownership inside the helper or document the exact
independent operation; parameter spelling does not establish safety.
Known local helper calls also retain SQLite handle, live-workspace and saved-method ownership,
including statically known tuple/list positional unpacking and dictionary keyword
unpacking, so a generic cleanup helper cannot hide closure of a shared database.
Direct synchronous local helper returns retain that ownership through aliases,
branches, helper chains and tuple unpacking. Shared-handle return patterns that
require coroutine, generator or recursive result evaluation are unsupported.
That rejection includes referenced shared handles captured from lexical or late
bindings, as well as handles passed as arguments; unrelated and shadowed names
do not establish shared ownership.
Ordinary resource and independently owned connection cleanup remain supported.
A helper that receives a known live workspace must establish explicit media mode
for a manual media seed; passing the workspace as an argument does not hide
missing or nested media ownership.
Writable PRAGMAs, including parenthesized forms and action PRAGMAs without
arguments and prepare-time settings inside `EXPLAIN`, require the same boundary
as other mutations.
Unannotated SQL callback parameters
and direct connection commit/rollback calls are checked conservatively too.
The same lock must cover direct reads, backups and transaction-state assertions.
It must also cover installing/removing SQLite trace callbacks, authorizers and
progress handlers, and changing the connection-wide `text_factory`. Keep cursor
consumption inside the lock. Release the lock
before HTTP calls: the request runs in a different thread and needs that lock.
The live-worker regressions in `tests/test_worker_sqlite_boundaries.py` enforce
these boundaries for the status/readiness polls and resumed intake upload.
For a rollback check, hold it across the rejected operation and the assertion so
an idle report worker's claim transaction cannot be mistaken for a leaked one.
The event-controlled regressions in `tests/test_full_text_synthesis_transactions.py`
overlap those fixture boundaries with a real report claim without stopping the
remaining workers or relying on wall-clock scheduling.

Resolve a matter's source store before entering entity/assertion source guards
and workspace transactions. Capture that store in validation and support
callbacks, including nested citation lookup. All source-cache acquisitions use
`source_cache_guard`, which takes the workspace lock before the cache lock,
including cached reads, archive access, purge and shutdown. Callers take the
matter source guard first and must not acquire another matter's source guard
inside the cache boundary. Shutdown detaches stores inside the boundary and
closes them outside it. Capturing the store does not cache validation
results: current source versions, passages, and membership remain checked under
the existing source guard and authorized transaction. The overlapping-action
regressions in `tests/test_workspace_lock_order.py` exercise this boundary.
Home's briefing snapshot must also pass its already resolved live store through
both readiness checks (including automatic-discovery progress) and source metadata
lookup. `CASE_INTELLIGENCE_TEST_MODE=1` enables `SourceCacheLock` ownership checks
before blocking, so an acquisition without the workspace lock raises immediately
and names `source_cache_guard`. Pytest and application CI enable this switch.
`tests/test_source_cache_order.py` covers inversion, contention, exception cleanup,
reentrancy and the matter-creation/Home call paths, and prevents production
callers from acquiring the cache outside the helper. Reentrant source/workspace
locks alone cannot enforce this cross-matter order. `tests/test_briefing_lock_order.py` overlaps
the real Home route with cached and uncached lookups, with discovery on and off.
Keep membership, completion and support rechecks inside the existing snapshot;
capture the store object, not an earlier readiness or authorization result.

The frozen Review acceptance pack pins test nodes and their complete files,
including helpers. Changes to those files require reviewed node/file digests
and an updated aggregate content fingerprint, even when case selection and
expected outcomes stay the same. Run `tests/test_review_acceptance_pack.py`
alongside the affected workflow tests when updating these fixtures.

Lease tests use the queue's injected clock for ownership deadlines and retain
real coordinator threads and SQLite transactions. Advance lease time only after
observing the required committed renewal or contention; use bounded monotonic
waits for thread progress. Wall-clock sleeps must not decide whether a synthetic
lease survives. Expiry cases explicitly cross the observed durable deadline and
verify that the stale worker cannot renew or save.

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

## Parallel worker diagnostics

The application job keeps six workers, the 360-second per-test faulthandler
deadline and disabled worker restarts. It retains bounded JSON resource snapshots
beside its JUnit report. For a local parallel run, add
`--worker-diagnostics-dir=/tmp/synthetic-worker-diagnostics` with a fresh directory.
The diagnostics record the active test function (without parameter values), test
phase, raw local-worker return code, signal, sampled process-tree RSS/thread/file
descriptor peaks, runner limits and cgroup v2 memory/OOM counters where available.
They do not read environment values, process arguments or fixture contents.
Sampling is once per second, so peaks are lower bounds; missing process/cgroup
data is unavailable, not zero. Incomplete process metrics are null, with per-metric
partial flags and separately labeled observed lower bounds. Missing task/child
access or the 512-thread/128-process collection caps mark the tree incomplete.
Peaks use only complete measurements and retain counts of partial/unavailable
samples; gaps never become zero measurements. Cgroup counters cover the group and its ancestors,
not an individual PID. A positive OOM delta is group evidence; SIGKILL alone does
not establish OOM. Exit code 1 alone does not establish a timeout: correlate it
with the existing `Timeout (0:06:00)!` thread dump in the job log.

`python -m pytest -q tests/test_worker_diagnostics.py` includes bounded child
suites for explicit exit, SIGTERM, a one-second faulthandler deadline, an ordinary
assertion failure and success. Each child uses one worker with no restart and a
30-second outer deadline. The first four must return failure; the parent verifies
their exact diagnostics. It does not force OOM or turn a real application failure
into a pass. These checks also run in the full application suite.
Additional proofs kill a worker during teardown and after its last test has
completed. A completed protocol clears its active marker. A worker crash during
session shutdown still fails the overall run even if xdist has no remaining test
item to fail; the diagnostics plugin changes only a would-be success to failure.
The observer retains its own clock callable so finite application-test clocks
are not consumed by phase reporting; the hosted-command regression covers this.

## Standalone browser journeys

On PRs changing browser journeys or their shared harness dependencies
(see [CI scope](QUALITY_GATES.md#ci-scope)), and on
merge groups, schedules and manual dispatch, the quality workflow also runs the synthetic intake and Report/export-readiness
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
  --junitxml=/tmp/exculpata-postgres-results.xml
python scripts/check-postgres-test-report.py /tmp/exculpata-postgres-results.xml
```

The tests mutate the database; use synthetic disposable data only. The service
and data are discarded with the hosted job. No deployment credentials, models,
or external model worker are used. Learned-model acceptance is explicitly
excluded and remains separate. This job exercises the existing hybrid backend;
it does not add a PostgreSQL exact-search adapter or complete the shared-corpus
PostgreSQL acceptance required by slices 00/08.
