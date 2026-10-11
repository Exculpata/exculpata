# Synthesis from one terminal full-text review

The first slice-19 adapter turns saved machine findings from one terminal
[full-text review](FULL_TEXT_REVIEW.md) into the existing
[hierarchical synthesis](HIERARCHICAL_SYNTHESIS.md). Reviewers can select
**Synthesize saved findings**, inspect supporting and competing accounts, open
their originals, and export the frozen input receipt with the result. This is a
bounded synthesis of admitted findings, not a claim that the entire matter or
every relevant fact was understood.

Scope contract: [issue #83](https://github.com/Exculpata/exculpata/issues/83)
and the [slice-19 brief](product-slices/19-full-text-synthesis.md). Scope
[PR #84](https://github.com/Exculpata/exculpata/pull/84) preceded
implementation [PR #85](https://github.com/Exculpata/exculpata/pull/85).
The [cruise receipt](EXIT_ALPHA_CRUISE.md#current-upstream-receipt) records
exact-head landing and acceptance. The
[dated acceptance](FULL_TEXT_SYNTHESIS_ACCEPTANCE_2026-09-12.md) preserves
synthetic scope, recovery evidence and the default-model limitation.
Supported-release and deployment qualification remain separate.

## Reviewer workflow

A compatible full-text run must be terminal: succeeded, failed, or cancelled.
Its saved findings can support a partial synthesis even when other ranges failed
or were never processed. Queued or running reviews, legacy ledgers, a missing
criterion version, and a run belonging to another matter are refused.

The action uses the selected run's complete criterion and its frozen source
population. Instructions remain unchanged when guidance is empty; otherwise
instructions and include/exclude guidance form one labelled canonical question. There is no new question, search, combined-run selection, or
additional review pass. The complete criterion must fit the existing 2,000-character
question limit, including any labels; longer input is refused without truncation.
The complete question reaches each issue- and matter-stage request. Stage
instructions live separately in working context, without a shortened duplicate
of the criterion. The action
uses the existing CSRF-protected form and includes the displayed input snapshot,
so a human edit between viewing the page and submitting cannot silently change
the admitted input.

Admission uses the existing actor/matter/request-key uniqueness in the same
transaction as input validation. Concurrent replay of an identical submitted
request returns one job; incompatible reuse refuses. A separately keyed request
lets the reviewer deliberately start a new bounded synthesis. Creation,
cancellation, retry and export retain the existing content-minimized audit
events: actor, matter, job and operation metadata, without criterion, evidence
or human-note text.

The result opens in the existing research-job inspector. Its **Frozen review
input** card shows admission counts, coverage gaps, human-decision context, and
links to the selected review and range ledger. The expandable receipt lists
omission reasons and the complete saved input metadata. Issue and matter nodes
retain original-source links. An invalid or stale checkpoint does not display
derived text as current evidence. If AI is unavailable, reviewers retain the
full-text ledger, manual decisions, and original-source navigation.

Direct JSON, Markdown, and Word exports retain the input receipt, hierarchy,
partial-result notices, and original citation identities. Copying or compiling
this adapter's result into a Report is unsupported in this first substep: its
choices are hidden and backend conversion refuses before generation or saving.
The generic Report paths cannot yet preserve the complete frozen input receipt
and source population. Existing full-text review material in Reports is unchanged.

## Frozen input and original evidence

Admission reads the selected run, criterion version, source and unit inventory,
range outcomes, counters, budget, and human decision revisions in one consistent
workspace read transaction. Canonical table digests and a snapshot digest bind
the receipt to that basis. The decision digest includes revisions and notes;
checking only the review's `updated_at` would miss some human edits.

Human decisions and their counts are recorded as `frozen_context_not_evidence`.
They neither select machine findings nor become source evidence. The receipt
preserves this historical context even when a reviewer later edits a decision.
Saved machine rationales are independently checked against the located original
unit before admission. Only the original passage supplies evidence to generation;
a rationale or an earlier summary cannot substitute for its source.

Original locators bind the matter, document, source version, unit, location,
excerpt digest, and full unit length. The application validates actual stored
source text for every frozen ready source, including sources whose findings were
omitted or whose ranges yielded no finding. Source validation is bounded and
batched. One scan budget spans all batches, including unsealed sources, and
resolves admitted citations during that same pass. Preparation's original
resolver uses the same limits. It does not expand an existing search result or
materialize the entire matter's parsed units.

A selected source set must still have exactly its frozen membership. Adding or
removing a member, changing a source version or content basis, losing a source,
or losing access refuses affected work. A missing or incompatible original
locator is an error; it is not repaired with generated prose. Selected-set scope
is rechecked after the original scan, so a membership edit during hashing refuses
the response as stale.

Each successful source-validation pass permits at most 10,000,000 text
characters, 20,000 units and 128,000,000 serialized JSON characters. Their
maximum UTF-8 sizes are 40,000,000 and 512,000,000 bytes respectively. A final
read buffer or decoded unit can exceed its aggregate allowance and trigger
refusal; oversized text is refused before an additional UTF-8/hash copy.
The reader also bounds one JSON record to 120,065,536 characters, accommodating
the escaped representation of a unit within the text limit. A cooperative
five-second deadline is checked during reads of at most 65,536 characters,
decoding and unit iteration; it is not an operating-system hard I/O timeout.
Exceeding any limit refuses admission or derived output in full, while the saved
review and originals remain available. No prefix is accepted as a verified
source population. Preparation and full-population validation are separately
bounded passes; five seconds is not a budget for the complete HTTP request.

## Admission and partial coverage

Version 1 has these fixed adapter bounds:

| Bound | Value and behavior |
| --- | --- |
| Retained candidate findings | At most 48 in saved range order; later candidates receive `finding_limit` |
| Complete original unit | At most 6,000 characters; an oversized unit is omitted in full |
| Prepared input | At most 1,000,000 encoded bytes; whole findings can receive `input_byte_limit` |
| Frozen receipt | At most 400,000 encoded bytes; an oversized receipt refuses admission |
| Frozen source population | At most 1,000 sources; exceeding the bound refuses admission |
| Enumerated ranges | At most 20,000; exceeding the bound refuses admission |

The 48 retained candidates are a conservative preparation bound. A retained
candidate later rejected by source verification does not permit unbounded
backfilling from the remaining ledger.

Every processed `include` candidate has one inspectable outcome: `admitted`,
`unsupported_finding`, `oversized_original`, `finding_limit`, `input_byte_limit`,
or `unsealed_or_invalidated`. Oversized entries also retain their range cursor,
document, unit ordinal, and full character count. The adapter does not send the
first 6,000 characters of a longer unit and imply it sent the whole original.
Decisive support or contradiction after character 6,000 is therefore explicitly
omitted with that unit, rather than silently lost or replaced by its rationale.

Coverage separately preserves no-finding, failed, pending, and invalidated range
counts; source and unit states; empty units; sealed sources with zero text units;
and the original review's budget and stopping reason. A no-finding outcome is
not affirmative evidence that a fact is absent. Omitted candidates or coverage
gaps mark the input partial. Even when every admitted finding completes, that
status does not establish whole-run factual recall.

The adapter reuses the existing hierarchy without increasing its capacity:

- At most 12 issue groups and 12 matter sections, with up to four parent claims
  per group.
- At most 12 originals, 6,000 characters per original, and 48,000 original
  characters per generation request; 1,200 output tokens per provider attempt.
- At most 32 charged generation-service requests. Each permits at most one
  existing verification repair, for at most 64 provider attempts with a
  1,200-token output limit each. The saved charged count is service requests,
  not measured provider attempts.
- A 900-second wall-clock deadline from the first synthesis start, including
  restart downtime. The deadline is checked between requests.

Both stages verify generated claims against original passages and retain the
original citation chain. These deterministic checks reject specific unsupported
content; they do not establish semantic truth. Representative model evaluation
remains a separate requirement.

## Durable work, changes, and deletion

Each generation request is charged before dispatch. Completed issue and matter
nodes are saved before subsequent work. Resume reuses valid nodes and repeats
only uncommitted work within the remaining request and time budgets. An
interruption, failed attempt, or retry does not refund spend or reset first start.
Cancellation and restart recovery acquire the database write lock before
reloading saved JSON, preventing an overlapping worker checkpoint from being
overwritten with older spend or nodes.
An exhausted result remains explicitly partial; the adapter does not add new
search passes or capacity to a completed traversal.

Admission, request boundaries, checkpoint writes, retry, and final saving validate
the frozen input and the worker's current attempt under the application's source
mutation guard and workspace transaction. Cancellation, a human decision edit,
source or source-set changes, access revocation, or a superseded attempt cannot
race a late write into accepted completion. Existing generic research-job write
methods also enforce the adapter boundary.

Deleting the selected review before admission prevents creation of the synthesis
job. Deleting it during synthesis prevents subsequent accepted writes while
preserving charged work in the research job. After completion, the historical
synthesis can still be read and exported with its unchanged frozen receipt after
human edits or deletion of the original review, provided the actual reader remains
authorized and every frozen source and selected-set membership remains valid.
Inspector and export responses recheck this source and reader boundary after
rendering, with the final membership check after the original-source scan.
An active synthesis prevents matter purge; terminal synthesis rows participate
in the existing purge lifecycle, and late workers cannot recreate deleted work.

## Storage and implementation boundary

`plan.full_text_synthesis_version = 1` identifies the adapter. Its existing
research-job JSON result contains `full_text_synthesis_input`, canonical `passes`
and `evidence`, and `hierarchical_synthesis`. No SQL migration, new table,
generation default, or model revision is introduced. The reserved adapter keys
are recognized by presence even if a version is missing or invalid. Such records
refuse processing, retry, save and derived output; they cannot enter legacy paths
that would erase their receipts or charged work.

- `FullTextSynthesisRepository` owns bounded snapshot reads and transactional
  receipt validation using the injected workspace connection and authorization.
- `FullTextSynthesisService` prepares original-backed inputs and invokes the
  shared hierarchy. Pure `validate_prepared` checks receipt structure,
  accounting, canonical passes, original digests, and input binding.
- `Workbench.queue_full_text_synthesis(matter, actor_id, run_id, request_key)`
  supplies application authorization, source guards, durable job coordination,
  and the optional expected page snapshot. Processing and completion use the
  existing research worker lifecycle.
- `POST /matters/{slug}/full-review/{run_id}/synthesize` queues that action and
  opens `/matters/{slug}/research?job=...`.

Take a consistent backup of the complete runtime before upgrading, following
[STORAGE_AND_BACKUP.md](STORAGE_AND_BACKUP.md). The new records are versioned JSON,
but an older writer does not understand the adapter's durable work. For rollback,
finish or cancel active work, preserve an upgraded backup and exports, and restore
a matching pre-upgrade backup with the earlier code. Do not treat the absence of
a SQL migration as permission to run an older writer on upgraded runtime data.

The [restore drill](../scripts/full-text-synthesis-restore-drill.py) creates a
synthetic terminal review with the earlier reader, backs up the stopped runtime,
interrupts synthesis after durable work, restores to a clean path with the old
paths unavailable, resumes, and reads originals and all three exports. It also
opens a separate matching pre-upgrade backup with the pinned earlier reader.
This is local SQLite recovery evidence, not a deployment or PostgreSQL recovery
qualification.
