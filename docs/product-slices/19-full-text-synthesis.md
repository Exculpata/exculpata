# 19: Synthesize one full-text review with original support

Status: **Done — first single-run adapter only.**
[PR #85](https://github.com/Exculpata/exculpata/pull/85) landed at
`95f290bf350742bc77c915eb0269c774069f3633`; the
[cruise receipt](../EXIT_ALPHA_CRUISE.md#current-upstream-receipt) records
final-head acceptance and passed independent post-merge checks. See the
[feature contract](../FULL_TEXT_SYNTHESIS.md) and
[dated acceptance](../FULL_TEXT_SYNTHESIS_ACCEPTANCE_2026-09-12.md).
Depends on landed 12 and 13; contract
[#83](https://github.com/Exculpata/exculpata/issues/83).
Future work below remains unscheduled. Capacity defaults and production model
settings are unchanged.

## User outcome

After reviewing all extracted text for a criterion, a reviewer can use the saved
findings to produce a cited synthesis for that same criterion. They can inspect
support, competing evidence, unprocessed ranges, and omitted findings. They do
not have to ask a fresh ranked search to rediscover what the review already found.

## First implementation boundary

Accept exactly one terminal full-text run, its frozen population, criterion,
revision, and authorized matter. Define a versioned input receipt binding source
and extraction identities, coverage, findings, and original locators. Resolve
original text through the existing source verifier; a range decision or earlier
summary is not itself evidence. Refuse incompatible, active, or stale input. Preserve the full criterion,
including instructions and include/exclude guidance, in a bounded canonical
question. If it exceeds the existing 2,000-character synthesis limit, refuse it
explicitly; silently slicing any criterion field is not permitted.

Stream and admit inputs within explicit existing resource limits; account for
all candidate findings and omissions before any completion claim. Do not load an
unbounded ledger into memory or hide truncation behind the existing hierarchy's
small policy. If the selected run exceeds what this first adapter can admit,
show the exact limit and unresolved work without claiming whole-run synthesis.

Pass validated supported findings through the shared hierarchy. Preserve
negative/no-finding outcomes and analysis/extraction failures in coverage even
when they contribute no generated claim. Keep machine findings and human source
decisions separate. Persist enough input identity to invalidate reuse after a
source or relevant run revision changes, with charged calls surviving restart.

Expose one action from the terminal review and reuse the synthesis inspector and
exports. Cancellation, source deletion, membership revocation, and source-set
changes must retain the existing save-time and response authorization boundaries.
Document whether human decisions are frozen context or a later view; do not let
a mid-run edit silently change the synthesis basis.

The synthesis action must require the existing CSRF dependency. Input-ledger
deletion must share atomic snapshot/dependency admission or invalidate active
synthesis at its next guarded write; saved charges cannot disappear. Active
synthesis must refuse matter closure, and completed/cancelled receipts and
checkpoints must join complete matter purge. Late workers cannot recreate them.

## Synthetic acceptance

The [dated receipt](../FULL_TEXT_SYNTHESIS_ACCEPTANCE_2026-09-12.md) records these
checks. The long-original case proves explicit whole-unit omission over 6,000
characters; it does not claim recall of the omitted late statements.

- Distribute supported findings and a late contradiction across more than 12
  extracted units. Require original support for both accounts in final output.
- Include an excluded/no-finding range, failed extraction, failed classification,
  empty source, and unsupported saved text. All remain correctly accounted for.
- Reject an active run, mismatched matter, changed revision, missing original,
  and stale source version; preserve authorized historical output appropriately.
- Exercise maximum-size instructions and include/exclude guidance: all terms
  reach generation unchanged within the bound, or admission explicitly refuses.
- Revoke the initiating member during an in-flight generation call. Subsequent
  checkpoints/final saves and status/export responses must deny access without
  refunding already charged work.
- Interrupt after one completed intermediate; resume without duplicating saved
  nodes or refunding spend. Exhaust a limit and verify honest partial output.
- Verify the browser path from terminal run to synthesis, original passage, and
  export, including keyboard access and understandable recovery.
- Reject missing/invalid CSRF on synthesis requests. Exercise creator deletion
  of the terminal input ledger during admission and generation, active-work
  close refusal, complete purge and late-worker rejection after purge.
- Version persisted records; restore a consistent backup into a clean database
  and resume/export. Document rollback compatibility even without a SQL migration.
- Run a representative pinned local-model evaluation separately from deterministic
  plumbing tests. Measure recall and false claims, including competing accounts.

Future work: multiple-run aggregation, partitioned review, adjustable capacity,
and new questions over a completed collection. Each requires its own scope and
acceptance; none follows automatically from this adapter.
