# Security model

Exculpata assumes confidential case material and a trusted internal operator.
It does not assume uploaded files, browser headers, model output, or model hubs
are trustworthy.

Primary controls include authenticated server-side sessions, CSRF protection,
matter-scoped authorization at the data layer, trusted-proxy secrets, stream
malware scanning, bounded parsers/subprocesses, non-symlink storage rules,
private companion-service networks, offline model workers, immutable model
revisions, transcript/answer provenance, and content-free operational health.

The gateway is the only published service. Default binding is loopback. Local,
OIDC, and Kerberos modes require secure cookies and trusted TLS. Real secrets
are file-backed and excluded from Git. The installer never prints or retains a
Hugging Face token.

The publication gate scans both the working tree and available Git history for
private addresses, personal paths, credential material, secret-shaped literals,
and operator-supplied internal deny terms. It also inspects extracted PDF
metadata/text and members of Office/ZIP containers under fail-closed expansion
limits. A clean current tree is not sufficient if residue exists in an earlier
commit.

Models can be wrong, transcripts can mishear, diarization can miscluster, and
retrieval can omit relevant material. The UI must preserve citations,
limitations, coverage, excluded-source notices, and direct source review. Do
not represent a generated answer as an exhaustive review. **Check selected
passages** evaluates retrieved passages within each frozen source. **Review all
extracted text** records every available text range and preserves extraction and
analysis gaps; neither establishes recognition of every relevant fact.

Manual **Close matter** is a deliberate application action: either the matter
owner or an authenticated Exculpata administrator may request permanent
deletion. Cross-owner administrator action is labelled explicitly and is
attributed to that administrator. The close workflow keeps a final export
available, refuses active work, requires exact-name and permanent-deletion
confirmation, preserves originals outside managed matter storage, removes the
complete workbench projection, and retains only content-minimized audit and
closure records.

Scheduled retention can delete a matter without another confirmation. New
matters default to 30 days of review followed by seven days of export grace;
enabled maintenance can then claim deletion when no work is active. Its first
pass runs at application startup. Restored backups retain their original
deadlines and may already be due, so hold maintenance in the isolated target
before starting it and follow [expired-matter recovery](EXPIRED_MATTER_RECOVERY.md).
Owners and administrators can extend active matters. Matters without a stored
schedule remain unscheduled.

Report vulnerabilities privately under [SECURITY.md](../SECURITY.md). Do not
attach case files, transcripts, credentials, or internal topology to a report.

## Rendered clip downloads

Clip downloads recheck the reader's live session, provider authority, principal
and matter access after rendering, before returning the file response. Rendering
does not hold the workspace lock. This check cannot retract bytes already sent,
and access changes after the final check do not interrupt an admitted stream.
Each download owns a separate temporary directory and deletion lease through the
outer response send. Normal completion, disconnect, cancellation and exceptions
all attempt cleanup and release that admission; another download's files remain
owned by its own lease. Filesystem cleanup failures retain the existing strict
matter-close recovery and startup reconciliation behavior.

## Matter naming

The [matter settings contract](MATTER_SETTINGS.md) permits owner/admin rename
under the existing authenticated authority. Ordinary members cannot rename.
The current-name comparison, active principal/matter and owner membership
checks, update, and content-free audit share an immediate control-database
transaction. Exact-name deletion confirmation serializes with that transaction.
No source identities, access grants, or external files change.

## Shared note mutations

[Case-note editing](CASE_NOTE_EDITING.md) rechecks active principal, membership,
matter and item identity with the displayed update token inside an immediate
transaction for edit, review and deletion. Tokens convey no access. Missing or
stale forms cannot overwrite, approve or delete newer text; recovery preserves
submitted edits only within the authorized matter. No note text is added to audit.

## Shared Report mutations

[Report editing](REPORT_EDITING.md) rechecks active principal, membership,
active matter and exact Report/section identity inside the same immediate
transaction as displayed-version/status checks and mutation. CSRF-protected
forms cannot bypass guards by omitting tokens. Recovery reauthorizes the matter
before showing saved work or a submitted draft; deleted work is not recreated.
Existing success audit contains operation metadata, without old/new Report text.

## Shared source validation

[Team source validation](REVIEW_VALIDATION.md) requires the displayed decision
revision and checks active principal, membership, matter and exact run/document
inside one immediate write transaction. Pending decisions cannot be adjudicated.
Recovery reauthorizes the matter before showing current or proposed human work;
deleted/unavailable reviews do not become new rows. Machine completion and
existing source-change invalidation serialize with human edits and advance the
same revision metadata. Success audit retains operation metadata without notes.

## Selection receipts

[Selected-file receipts](INTAKE_RECEIPTS.md) retain browser-reported inventory
and selection-level exclusions. They explicitly identify that origin in the UI,
API and downloads; they do not attest historical capacity or the contents of a
file chooser. Independent server filename checks, authoritative upload admission,
received-byte counts and exact source availability remain separate. Browser
review states never authorize upload. Receipt metadata has atomic cumulative
matter, creator and workspace limits. Only owners may discard receipts with no
received data, including cancelling bound empty upload attempts. The same source
mutation lock covers chunk writes/offset commits and cleanup; cleanup checks saved
partial files as well as control rows. Requests recheck upload state after reading
their body and acquiring that lock, so a delayed request cannot write after cleanup.

## Local account changes

[Local account mutations](LOCAL_ACCOUNT_LIFECYCLE.md) retain the owner-only
secrets-file boundary and the application's read-only mount. A process writer
lock serializes fresh reads, last-enabled-administrator validation and atomic
replacement. Every local session resolution validates the opened file metadata,
reuses only an unchanged validated snapshot, and checks its revision binding.
Password, enabled-state and role changes invalidate
previous sessions without requiring a process restart. Version 1 migration
requires an explicit verified backup. Restoring that exact v1 backup requires the
operator rollback command with the application and all account writers stopped;
it durably invalidates local sessions before replacing the account file. Live revision
rotation alone cannot prevent revival after raw-copying a v1 account binding.
The optional
[browser account profile](LOCAL_ACCOUNT_BROWSER.md) grants writes only to the
dedicated canonical account directory. Browser mutations require CSRF and fresh
administrator session validation under the account lock, enforce keyed edit
preconditions, and audit the actor before replacement. Existing secrets remain
read-only; provider authentication and trusted-proxy boundaries are unchanged.

Account path validation covers every opened ancestor before descending; only
root/service ownership with non-writable ancestors is accepted, apart from
root-owned mode-1777 intermediate directories. Browser rename projection and
direction-specific completion audits share the writer lock with the mutation.
Frozen browser-account backups and clean restores validate the required store,
permissions and shared bounded format before claiming success.


## Full extracted-text resource boundary

[Full-text review](FULL_TEXT_REVIEW.md) admits frozen character, call and ledger
reservations under an immediate SQLite transaction across actor, matter and
instance scopes. UTF-8 metadata charges include retained work; classifier calls
are charged before dispatch and survive cancellation/restart. Recovery/retry
control growth is charged, and terminal creator-only deletion releases retained
capacity. Attempt fences and streamed source/citation revalidation protect late
worker saves. Constant-size counters expose unresolved extraction and zero-unit
gaps without polling the full range ledger. Legacy unbudgeted runs remain readable
but cannot resume; malformed locators remain in exports and cannot become links.

## Application team groups

[Reusable team groups](TEAM_GROUPS.md) documents explicit matter grants, live
revocation, provider boundaries, migration 0031 and backup/rollback constraints.

## Source-backed events and assertions

[Events and assertions](EVIDENCE_ASSERTIONS.md) reuse the authorized entity unit
of work and source mutation guard. Every write checks the displayed record
revision; role attachment/replacement checks the explicit entity revision.
Attribution, original support and human review remain separate. Historical
source or identity snapshots never regain current authority through export or
reconciliation. Matter purge removes records, roles, accounts and history.

## Single-run full-text synthesis

The [adapter](FULL_TEXT_SYNTHESIS.md) binds one terminal full-text ledger, exact
criterion, source scope and decision-inclusive revision in a consistent read
snapshot. Human decisions are frozen context, never evidence. Only claims checked
against original passages enter the shared bounded engine; oversized originals
are explicitly omitted. Admission, retry, checkpoint and final save revalidate
the input under the authorized transaction and source mutation guard. Deleted
ledgers, edited decisions, revoked access, changed originals and expired worker
attempts refuse further writes while retaining charged requests.

The synthesis action requires CSRF validation and ordinary matter membership.
Inspector/export responses recheck the actual reader and all frozen source
identities, including uncited or omitted sources, after rendering. Completed
results may retain their historical human-decision context; historical source
identity never grants current access. Quarantined-source exports fail closed.
Active synthesis uses existing research-job close/backup guards; matter purge
removes the jobs, receipts and nodes, and late workers cannot recreate them.

## Explicit reviewer context selection

Continuity B uses matter/principal-owned reference selections, not a new access
boundary. Stable object IDs are matter-checked through the existing authorized
services under source-then-workspace guards. Selection revision and current
object/dependency approval stamps fence writes. Changed, missing, unsupported,
disputed and dismissed records do not become accepted facts through selection.

B's in-transaction session recheck uses the identity resolver's read-only mode:
current session expiry/revocation, principal, local-account binding and provider
authority are still checked, but the resolver does not touch sessions, refresh
principal projections or commit the caller's transaction. Normal request
session maintenance retains its existing behavior. Rendered previews/conflicts
are reauthorized before return. Administrator visibility remains read-only
without membership. CSRF applies to selection writes; GET does not create
selection or knowledge state. B calls no model and changes no answer inputs.

## Recorded context in focused answers

Continuity C is explicit opt-in on the existing queued focused-answer route.
The existing source-then-control lock order binds the approved selection to the
request key. Replays return that snapshot. Source-set membership, every required
original, current principal/provider authority, cancellation and worker attempts
fence dispatch and final save. No write lock extends through inference. Stale
workers cannot mark a newer worker's answer failed. Historical snapshots are not
current authority and survive knowledge edits only within the existing matter
retention boundary.

Typed context is untrusted orientation. It cannot merge identities, confirm
assertions, change access or supply evidence. Whole supporting/competing groups
must pass scope, version, kind and budget checks together. The existing verifier
still applies; C additionally requires each accepted claim/limitation to match a
contiguous original span, after the existing transcript attribution handling.
This deliberately limits paraphrasing. Prompt injection fixtures and span checks
do not prove general model safety or usefulness. Runtime tokenizer and inference
requests have separate immutable receipts, with actual transport states and
returned metadata. Credentials/headers and case text never enter general logs.
