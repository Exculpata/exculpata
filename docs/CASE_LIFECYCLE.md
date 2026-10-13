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

## Owner workflow

1. Open **Manage matters → Close matter**. Finish pending uploads, processing,
   reports, answers, investigations and downloads. **Begin closure** freezes
   source and content changes and starts a minimum seven-day cancellation window.
2. **Prepare full export** creates a private, disk-backed preservation ZIP. Its
   manifest records every included file, byte length and SHA-256 digest, source
   inventory, table coverage, explicit exclusions and unavailable historical
   originals. The service reads and verifies every member before registering the
   export for that exact closure.
3. Download the archive and retain it outside the workbench. Compare its SHA-256
   using a trusted local tool with the digest shown here before executing its
   included verifier, then inspect the manifest and verify all members.
   Downloads can be resumed with HTTP byte ranges
   and retried. Preparing or downloading does not count as an acknowledgement.
4. After seven days, request **Review final deletion confirmation**. The form
   requires the exact current matter name, a separate acknowledgement that the
   export was downloaded and checked, and a permanent-deletion acknowledgement.
   Its single-use confirmation expires after ten minutes and is bound to the
   current actor, live session, matter name, closure, export and content digest.
   The application rechecks content and the stored archive before the first
   destructive claim. A date, old form, work-product ZIP or restored receipt alone
   cannot authorize deletion.

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
but excludes originals and is not the preservation archive required for exit.
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

## Migration 0040 and recovery

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
New matters are ongoing with a 90-day check-in. Already purging, failed or deleted
matters are not silently reopened or assigned a fictitious export.

The schema fences old writers, including an already-open old connection. An old
application cannot open the upgraded lifecycle schema. This is defense in depth,
not a substitute for stopping old workers before migration: filesystem actions
outside the database cannot be undone by a trigger. Rollback requires the
matching pre-upgrade application and its untouched backup restored into a clean
target. Never run old code against the upgraded store. Preserve post-upgrade work
separately before deciding to roll back.

A restored pending closure keeps its original cancellation date, frozen content,
export identity and digest. Missing or damaged server-side archive bytes block
a first purge claim. Restore accepted originals and the archive, or cancel,
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
