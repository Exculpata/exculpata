# Isolate the row budget parity test

## Kickoff

- Goal restated: Run the 10,000-row rebuild budget test once, alone after both
  application shards, in hosted CI and workflow-derived local parity. Contributors
  receive a reliable timing result without shard contention; all other discovery
  index tests remain in the shards and both approved ceilings remain unchanged.
- Expected files: the quality-gates workflow, application shard selector, shard
  and parity tests and snapshot, exact parity setup mappings, quality-gate docs,
  and this PR body source. Leave the parity runner and product code unchanged.
- Acceptance check: first demonstrate failing scheduling/selection regressions;
  then pass focused contracts, real pytest collection and an isolated run below
  135 s. The launcher subsequently runs full parity and must report GREEN OVERALL,
  zero unexpected unavailable steps and no NEAR TIMEOUT results.
- Issue link: no issue URL was supplied for work item
  `2026-11-15-parity-row-budget-serial`; the user-provided scope and acceptance
  criteria are the task authority. Related work:
  [PR #524](https://github.com/Exculpata/exculpata/pull/524).

Recorded before implementation. The kickoff matches the supplied work item.
Starting commit: `7f9e2616f9b6aa41a8923a935bd8a87300576897`; retain all ancestors.
