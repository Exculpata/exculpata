# Make parity scheduling tests independent of host capacity

## Kickoff

- Goal restated: Eliminate PR #524's hosted-only overlap failure by controlling
  synthetic scheduler capacity and proving overlap with event synchronization.
  Contributors get reliable parity results on small hosts and a clean gate
  failure instead of a missing corpus-guard script error.
- Expected files: concurrency tests, application workflow and its guard/shard
  tests, parity plan snapshot and scenario helper if needed, this task, and
  local PR.md. Change the runner only if diagnosis proves a product defect.
- Acceptance check: reproduce the parent failure with four-CPU affinity; prove
  overlap with explicit capacity at CPU counts 1, 2 and 4 under four xdist
  workers for 20 repeats; exercise failed-gate and post-checkout guard behavior;
  pass focused contracts and the private-terms scan. Preserve both serial nodes,
  their order, and the 125 s / 135 s ceilings. Launcher owns full parity.
- Issue link: the supplied fix-up is the scope authority; no separate issue was
  supplied. Related [PR #524](https://github.com/Exculpata/exculpata/pull/524).

Recorded before coding against c8d56a3c00734d0273ec6b3c81e11babdb9a07b1.
The PR remains draft; comments record the parent pass and no conflicting hold.
This kickoff matches the supplied scope. No push or merge is authorized.

## Verification

The parent mocked CPU count but retained the host affinity cap. Restricting the
preserved parent test to four CPUs reproduced its overlap failure; the runner
correctly admitted one four-worker cell. Both parent application workflow
probes also failed because the corpus check ran after checkout was skipped.
The historical failing commit is c8d56a3c00734d0273ec6b3c81e11babdb9a07b1.

The new event-controlled test rejects a deliberately serialized four-worker
budget immediately. With an explicit eight-worker budget, 20 invocations at each
physical CPU affinity of one, two and four passed under four xdist workers.
Every invocation forces reported CPU count and affinity to 1, 2, 4 and 8:
240 passing cases, zero failures. Separate budget tests preserve the real
count/affinity minimum. No production scheduler change was warranted.

The updated workflow snapshot passed; the focused parity, shard, corpus guard
and fixture-policy suite passed 2,344 tests. All 34 concurrency tests also passed
on one physical CPU under four xdist workers. The serial invocation, its shard
exclusions and both duration ceilings are unchanged. Full parity remains the
launcher's follow-up. The local PR.md records commands and CI evidence.
