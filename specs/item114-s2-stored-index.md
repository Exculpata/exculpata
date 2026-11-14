# Stored discovery review index, item 114 slice 2

## Kickoff

- Goal: store a rebuildable discovery projection so reviewers can browse pages of
  100 documents without rebuilding or sorting the full matter on each request.
  Every source remains reachable and incomplete indexing or filtering is visible.
- Expected files: exactly the supplied A8 fence, with index migration 0049 in both
  mirrors; the additive D9 Coding reads and post-commit hook; the one D9b bounded
  identifier read; and the single archive-exclusion sentence in CASE_LIFECYCLE.md.
- Acceptance check: every item remains reachable; completed traversal has no
  duplicates or omissions; unchanged sources are not reparsed; pending metadata
  never becomes an absence claim; derived state survives restart reconciliation,
  stays outside archives, and disappears on purge; the portability test executes
  in the provisioned PostgreSQL lane. D11b v2 requires persisted version-2 cursors,
  at most 256 row touches per step and 128 attempts per GET, independent
  cancellation, and no index background execution. All A7 proofs remain required.
- Issue: https://github.com/Exculpata/exculpata/issues/503

Recorded before implementation. Issue scope and the supplied amended design
match this goal and acceptance check. Issue 503 has no comments or assignees.
Slice 1 is merged as b47fde3f. The parked draft 505 remains untouched. Its earlier
scope blockers are covered by the explicit D9/D9b authorizations; its background
execution failure is the root defect addressed by D11b v2.

The launcher requires the current job branch, local commits only, and no push.
The supplied checkpoint 0597095e0d82 exists and will be evaluated as optional
starting material. The base already contains migrations 0047 and 0048.

## Implementation

The implementation uses migration 0049 and four excluded, matter-scoped tables.
GET requests own synchronous steps; each committed cursor contains its phase,
source target, sequence, continuations, EOF markers and counters. No index thread,
executor, lifespan wrapper or automatic refresh remains. Preparation releases the
workspace lock. Capacity checks precede ownership, and cancellation uses the
independent jobs lock. Derived mutations and cursor advancement share a transaction.

Completed views use binary natural-order keys, bounded indexed prefixes and
complete-key tie resolution. Literal filters traverse candidate windows, including
empty windows. Pending views enumerate the source registry and preserve the requested
query in a manual refresh link. Pending/error presentation is separate from evidence
values on both Fields surfaces. Identifier POST selection still uses the complete
legacy reader. Coding and Team ID projection reads use bounded public seams.

Reconciliation preserves verified parsed-source caches, revisits load-row matching
and first-row precedence, and verifies human work before publishing an item. It
never publishes completion from count equality alone. A new source epoch resets
traversal markers. Archives exclude all four derived tables while retaining their
reviewed column inventories; explicit purge cleanup and existing lifecycle guards
cover them.

## Work envelope

Constants are 256 total row touches, 32 reserved control touches, 32 maximum
primitive touches, and 128 attempted steps per GET. Empty probes and phase
transitions consume conservative work credits. A full slice packs at least 192
work credits. Stable work completes within `ceil(W/192) + E + 2` steps. Capacity
preparation retries count toward both request attempts and accumulated work.

The generated manifest implements the design's R/F/L/A/C/I/V/Z/H/P/U/Q formula.
The stated maximum small fixture dimensions evaluate to W=4,132 (within the
reviewed W<=4,232 bound), E=76 and 100 steps (within 101). The production fixture
actually completed in 5 committed steps, 438 work credits and 2 early yields.
The 100-source cold/reconciliation probe recorded 61 steps and maximum 212
logical touches in one step. The 1,000-source run recorded 559 steps and maximum 210 touches; the 10,000-source
run recorded 5,540 steps and maximum 209 touches. These include a cold build and
a retained-cache reconciliation. The independent observer counts repository rows,
joined constituents, source/identifier/Coding seams, parser records and mutations.

These are row-transfer and mutation bounds. Source-byte verification/decoding,
large payload serialization, database plans, Coding change aggregates and large
identical-prefix buckets have separate costs. Cold registry startup and inherited
Coding controls and identifier operations remain outside the bound.

## Regression evidence

Starting material was the supplied checkpoint 0597095e0d82 applied onto the
current main-based job checkout, with conflicts resolved against main and the new
index migration renamed to 0049. The parked branch was not changed.

Before the scheduler rewrite, the actual checkpoint generator/repository probe
retrieved 400, 3,100 and 30,100 rows in one next() at populations 100, 1,000 and
10,000. Direct checkpoint witnesses failed the thread-origin, request budget,
sequence/cancellation, capacity placement, checker and pending-refresh contracts.
They identified the old Thread constructor and capacity call inside commit.

Historical replay also verified e6644a22's 100-row page called sort_key 301 and
3,001 times, and its unresolved dynamic field was classified absent. Its feature
checker discovered only flat modules. The new real execution checker rejects
0597095e0d82 at rebuild.py:39 and names stored_index.rebuild.Service.advance.
The checkpoint template lacks the new refresh anchor.

Evidence limitation: the initial checkpoint witnesses were source/protocol checks
and the real row-count probe, not executions of every named A7 regression. The
historical sort/absence/checker replay was collected after implementation. Full
before-change HTTP runs of every named test were not captured; this timing and
coverage gap remains for review rather than being described as passing fail-first
evidence.

## Verification record

- Repository gates: 2,426 passed, covering import/feature/checker boundaries,
  module registry, database/source-size/lifecycle checks, API snapshots, fixture
  policy/inventory, exit/purge conformance/consistency and browser-runner unit tests.
- Additional lifecycle, checker, fixture-policy and source-size checks: 617 passed.
- Focused index, updates, lifecycle, normal performance setup, list, mount, codes,
  archive, migration inventory, fixture-script and checker tests: 296 passed,
  one deselected (the prohibited opt-in million-item benchmark), zero failures.
- Expanded service reconstruction, atomic rollback, request-boundary resume,
  same-version metadata changes and frozen list/Coding checks: 87 passed.
- Final feature/checker, fixture-policy and source-size checks: 615 passed.
- Final small authoritative benchmark/reopen setup: one passed after adding
  opt-in qualification telemetry. The million-item path was not executed.
- The 15,000-code item remains pending after the first read and completes with
  all labels preserved. Its larger capacity reservation occurs outside locks.
  The concurrency probe observes storage waiting followed by one unsuccessful
  nonblocking ownership attempt and a committed-state snapshot.
- Frozen codes, archive, migration-inventory and fixture-script files remain
  byte-identical. Existing list, mount and boundary test lines are preserved.
- Fence audit found 37 authorized changed paths. Migration mirrors are identical.
- Ruff, git diff --check, the 613-contract rename check and rename-artifact
  parity passed. The added-lines private-terms scan passed. Working-tree
  publication scanning passed after removing disposable data and lint caches.

Not run: PostgreSQL portability and hosted JUnit execution evidence (no disposable
PostgreSQL DSN); TLC normal/mutant targets (no TLA2TOOLS_JAR); make check-fast
(no PostgreSQL DSN or pinned secret scanner); the million-item benchmark and
300 ms qualification (explicitly excluded by this task).

The browser acceptance command ran but failed when Chrome could not start;
there is no browser journey pass. The initial publication scan also failed on
disposable scratch artifacts and two historical findings. Disposable scratch
artifacts and lint caches were removed; the final working-tree-only scan passed.
The full scan reported the removed lint-cache files plus two historical blockers:
`76998dd5cb71:tests/test_network_contract_filenames.py` (private-network-address)
and `fbecc3a51132:tests/fixtures/publication/compressed-collision.gif`
(non-example-email-address). Those historical paths are outside this slice.
Publication tooling and historical commits are unchanged; no full publication
pass is claimed.

No merge, push, release, public message or PR publication is authorized by the
launcher. This local candidate retains the review gate and does not claim the
missing portability, browser, publication or fail-first evidence.

## Retry 3: capacity replay progress

Root cause: exact-prefix capacity reservations replayed a large projection once
per following write, allowing 128 attempts to expire without a cursor commit;
random document-ID ordering changed the number of following writes.

The repository now requests at least twice the previous reservation when an
encoded write exceeds it. The existing service still performs every capacity
check outside ownership and storage locks, rolls back the failed transaction,
and revalidates the target and cursor before replay. This conservative byte
estimate grows geometrically instead of one write at a time. Capacity refusal
still leaves honest pending/error metadata. Row and request budgets, complete
values, atomic cursor publication and the monotonic-progress assertion are
unchanged. The larger reservation can exceed the final encoded step estimate;
it does not allocate a buffer or bypass the real storage-capacity check.

The small synthetic regression failed against starting product code 24d5044587a4:
one oversized cache write followed by 160 small writes exhausted 128 attempts
without committing. With the fix, the same transaction commits in two attempts,
with 161 row touches; the failed attempt leaves the original cache intact.
The existing heavy-item test, its setup, 15,000 codes, progress assertion and
complete-value checks remain unchanged. No frozen/shared test was changed, and
no timeout or assertion was relaxed.

The ordered shard also exposed an independent fixture defect: the archive test
registered a partial namespace without `matters_root` as a service context.
Its subscription cycle could outlive the test, so a later unrelated purge raised
AttributeError while traversing the service registry. A new cross-root
cancellation check reproduced that failure directly against the fixture from
24d5044587a4. The fixture now constructs the real ModuleContext, whose constructor
requires the root, and cancels its job after the archive checks. The same test
proves that another root cannot cancel its job and its own root can. Production
already receives ModuleContext; weakening its purge boundary would mask an
invalid test input. The shared archive fixture and existing list tests are unchanged.

Retry validation: the unchanged named test passed alone and in the complete
ordered four-worker shard. The capacity and archive-context regressions failed
before their respective corrections and passed afterwards. The focused feature
run passed 252 tests; parity/checker/fixture/database gates passed 2,203 tests,
additional boundaries passed 14, and the final fixture/parity run passed 2,125.
The archive regression plus its three affected list/purge cases passed together.
Ruff, rename contracts/artifacts, diff checks, added-lines private-terms scanning
and working-tree publication scanning passed. These counts describe separate,
overlapping selections, not an aggregate test count.

Validation is BLOCKED before five consecutive acceptance runs. The complete
shard returned 14,627 passed, 10 failed, 13 skipped and two existing expected
failures, with 34 passing subtests. The named resume test passed. Nine failures
in `tests/test_time_to_briefing_benchmark.py` and `tests/test_verify_exculpata.py`
require temporary artifacts outside the checkout; one in
`tests/test_worker_diagnostics.py` expects a temporary test node outside this
repository's pytest root. The parent task requires all scratch data inside this
worktree. These test files and their supporting scripts are outside the fence;
none was edited, skipped or weakened. Earlier interrupted diagnostic runs are
not acceptance passes. The launcher retains responsibility for full CI parity.
