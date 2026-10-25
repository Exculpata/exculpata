# Ongoing matters, full export and explicit exit

Issues [#215](https://github.com/Exculpata/exculpata/issues/215) and
[#225](https://github.com/Exculpata/exculpata/issues/225): a matter remains active
until its owner or an authenticated administrator explicitly closes it. New
matters have a 90-day check-in. Check-ins are in-app reminders, not email or
external notifications. An overdue check-in does not suspend access, start a
countdown, or authorize deletion. Checking in moves the next reminder 90 days
forward; there is no maximum lifetime measured from creation.
In-app attention starts 14 days before the check-in, becomes a seven-day
reminder, and remains visible when overdue. At 30 overdue days it explicitly
asks the owner or administrator to review ownership and arrange a check-in or
exit. Opening or using the matter does not silently renew it, and a restart
shows the current stage without depending on reminder delivery while offline.
Lifecycle dates retain a UTC timestamp and show the viewer's local date, time
and zone alongside it when browser formatting is available. Eligibility still
uses the original aware UTC instant.

During a pending closure, every authorized owner, direct member and group member
sees a banner on full workbench pages, including when working on another matter.
It identifies an administrator-initiated closure, shows the earliest deletion
time and explains how to request cancellation. Owners and administrators get a
link to the closure controls; ordinary members receive no new write authority.
The banner remains visible after seven days until cancellation or final
confirmation. `matter.exit_notice` records the first presentation per exit and
viewer, with opaque identifiers and role only. It does not assert that the
person read or acknowledged it, and does not send an external notification.

## Owner or administrator workflow

1. Open **Manage matters → Close matter**. Finish pending uploads, processing,
   reports, answers, investigations and downloads. Choose **Export and exit** or
   **Delete without export**. **Begin closure** freezes changes and starts a
   minimum seven-day cancellation window. The choice is durable; changing paths
   requires cancellation and a new closure/window.
2. For **Export and exit**, prepare the preservation ZIP. The service verifies
   every member before registering the archive for this exact closure. Download
   it and retain a copy outside the workbench. Compare the shown SHA-256 using a
   trusted local tool before running the included verifier, inspect the manifest
   and verify all members.
3. The export path requires server-observed delivery of every archive byte to
   the account that will confirm deletion. Successful whole downloads and the
   union of completed HTTP byte ranges qualify. Aborted responses, overlapping
   repeats, gaps, another actor's download and replacement archives do not.
   Observation occurs after the outer ASGI response accepts the final byte;
   it cannot prove the client saved or retained a durable backup. A separate
   saved-copy acknowledgement remains mandatory.
4. **Delete without export** creates no preservation archive and requires a
   separate acknowledgement that the user intentionally accepts irreversible
   deletion with no export or backup provided by this workbench. A missing or
   incomplete export never silently selects this path.
5. After seven days, request **Review final deletion confirmation**. Both paths
   require the exact matter name and a permanent-deletion acknowledgement.
   The single-use form expires within ten minutes and binds the actor, live
   session, matter name, closure mode and applicable archive/content identity.
   Current owner/admin authority is rechecked when starting closure, confirming,
   claiming and executing destructive phases. Ordinary membership is insufficient.
   The export path additionally rechecks the archive and content snapshot before
   the first claim. Dates, old forms and work-product ZIPs cannot authorize deletion.

Local account authority is tied to its revision at fresh sign-in; disablement,
role/password changes and session revocation invalidate old deletion authority,
even after a disable/re-enable cycle. Kerberos rechecks current group policy.
OIDC administrators closing another owner's matter must sign in again for each
closure initiation and each final deletion attempt. The existing OIDC client
requests `max_age=0`; verified `auth_time`, fresh group claims and the same subject
are required. This uses the configured scopes and credentials without requesting
new grants. The authorization binds the subject, newly issued session, matter,
exit, closure mode and action, and expires no later than ten minutes after the
provider authentication (earlier token/session expiry also applies). A callback
only returns to the action page; the administrator must deliberately continue.
The central service checks this grant before claim and every destructive phase,
including direct service calls. A retry needs another fresh authorization.

A new verified sign-in invalidates that actor's older closure authorizations;
local session revocation, account disablement, configuration changes, expiry and
restart also block them. Provider directory-group changes are not queried during
the accepted ten-minute window: this is the approved bounded session-authority
policy, not instant directory revocation. Missing, stale or unsupported fresh
claims fail closed. OIDC owners retain their active owner-grant path. Check-ins,
exports and cancellation use ordinary current owner/admin session authority.

**Cancel closure and reopen** is available until final deletion is claimed. It
invalidates the closure's export and confirmation, removes its server-side
archive, and permits changes again. Starting another closure creates a new
seven-day window. Waiting beyond the window never deletes anything automatically.
Administrators see the owner and explicit administrator attribution. Ordinary
members cannot close, confirm, cancel or certify a full export.

## Preservation boundary

The full archive includes currently retained, accepted original documents and
media, full extracted text, retained playback renditions, source/version
provenance, conversations, notebooks, reports and their histories, research and
review ledgers, context selections, intake, entities, assertions, transcript and
speaker revisions, clip definitions, and failed/cancelled/incomplete outcomes.
Witness inconsistency review decisions retain candidate identifiers, reviewed or
dismissed state, matter-local actor attribution and update times. Their cited
accounts remain in assertion records; candidate wording is computed on demand
and is not stored as a separate result.
Raw authored text and stale historical citations are retained. Missing current
originals, unresolved staging/quarantine, unknown schema fields, unsupported
payload shapes, corrupt bytes or capacity limits block certification; they do
not produce a misleading successful subset. Previously removed originals are
identified as unavailable rather than recreated.

Global accounts, credentials, authorization grants, authentication sessions,
request/worker leases, host storage paths and operational diagnostics are
excluded. Attribution is matter-local. The export projects explicit fields;
it does not copy the control database or recursively ZIP storage directories.
The existing **work-product bundle** remains useful for presentation documents,
but excludes originals and is not the preservation archive used by export and exit.
Rendered clip downloads are temporary; retained originals and clip definitions
preserve their source and boundaries.

Archive creation and verification use bounded reads and disk-backed ZIP64
output. They respect the configured hardware profile's matter allowance and
protected storage reserve. Exhaustion, malformed data or interruption leaves the
matter frozen and available for recovery or cancellation, with no new verified
receipt. A large matter may require additional capacity within the configured
profile; there is no silent truncation or universal 100 MiB completeness claim.
Process death can leave an incomplete `building-*` directory within that
matter's preservation storage. Restart retains it; preparing a replacement,
cancelling closure or completing purge removes it. Startup does not remove a
potentially live export owned by another process. Include these directories in
capacity planning and stopped-runtime recovery inspection.

The archive is a portable preservation format, not an automated importer or an
application backup. It does not establish a legal retention period or guarantee
physical erasure from storage devices, downloaded copies or independent backups.

## Migrations 0040–0042 and recovery

Before upgrading, stop every old application and worker, take a complete,
consistent backup of the control database, source registries and payloads,
managed storage, identity/configuration material and configured PostgreSQL
projection, and perform a clean restore drill into an isolated target. Include
`preservation-exports` in managed-storage backups after this upgrade. Keep
backup contents private. Do not use a running instance as the migration test.

Migration 0040 preserves matter identities, content, original legacy dates and
lifecycle states. Existing 30-day or other dated active matters enter
`legacy_review`: their dates remain visible for reference but cannot trigger
purge. The owner or administrator explicitly selects ongoing operation with a
check-in. Previously unscheduled matters remain unscheduled until that choice.
Their owner/admin sees a prompt-only invitation in Manage matters and Matter
settings. **Enroll in 90-day check-ins** is a CSRF-protected, explicit choice;
viewing, ignoring or restarting never enrolls the matter. Ordinary members cannot
enroll it. Enrollment preserves matter content and creates no deletion schedule.
New matters are ongoing with a 90-day check-in. Already purging, failed or deleted
matters are not silently reopened or assigned a fictitious export.

The schema fences old writers, including an already-open old connection. An old
application cannot open the upgraded lifecycle schema. This is defense in depth,
not a substitute for stopping old workers before migration: filesystem actions
outside the database cannot be undone by a trigger. Rollback requires the
matching pre-upgrade application and its untouched backup restored into a clean
target. Never run old code against the upgraded store. Preserve post-upgrade work
separately before deciding to roll back.

Migration 0041 adds explicit closure modes, delivery coverage and durable purge
admission records. It preserves existing exits as export-and-exit and never
invents evidence of download or acknowledgement. Existing local sessions need a
fresh sign-in for closure. A previously confirmed 0040 purge without the new
authority record stays blocked for operator recovery, even when an earlier phase
removed its archive. The writer version and policy version prevent old binaries
from resuming these operations against the upgraded database.

Migration 0042 adds private, ephemeral OIDC request and authorization tables.
It creates no grants, enrollments or deletion authority. These records are
excluded from both the preservation archive and its content snapshot, and are
cleared at application startup/restore. Final cleanup removes them. An interrupted
OIDC administrator purge therefore requires fresh sign-in and confirmation after
restore, even when a saved authorization had time remaining.

A restored pending closure keeps its original cancellation date, frozen content,
export identity and digest. Completed delivery observations survive restart/restore; they still do not prove a backup exists. Application startup discards in-flight download attempts and unused deletion forms,
so a restart or restore requires fresh confirmation even if the old form and
sign-in session have not expired. Starting another application process can also
invalidate an outstanding form; request a fresh form on the running instance.
For export and exit, missing or damaged server-side archive bytes block a first purge claim. Restore accepted originals and the archive, or cancel,
repair while active, and begin a new closure/export. A restored check-in or
legacy date never authorizes a maintenance purge.

An interrupted deletion that was confirmed under this policy remains closed.
A fresh owner/admin confirmation retries the same purge identity; phases already
completed may have removed originals or the server archive, so retries use the
persisted verified pre-deletion snapshot and cannot reopen review. Deletion
started under the previous policy has no certified full-export receipt and is
blocked for operator recovery. Inspect an isolated backup and remaining storage;
do not manufacture a new receipt or silently adopt the interrupted operation.

Final deletion removes workbench content, processing copies, derived projection
and server-side preservation archives. External registered originals are
untouched. Minimal attributed closure and security audit records remain. No
maintenance or recovery worker initiates or retries matter deletion.

## Synthetic verification

Focused lifecycle tests cover elapsed-time boundaries, stale forms, session and
owner revocation, single-use confirmations, cancellation/re-entry, active work,
failed phases and concurrent claims. Archive tests cover every declared table,
original bytes, media revisions, unavailable historical references, privacy
canaries, unknown schemas, capacity failure, corruption and offline verification.
Authenticated HTTP tests exercise actual archive bytes, ranged retry, missing
restored archives, changed originals, read-only source opening and final cleanup.
Migration tests verify preserved dates and content, rollback on failure, blocked
old writers, and SQLite integrity after a synthetic backup and clean restore.
These tests do not certify an installation's own backup, legal obligations or
production deployment.
