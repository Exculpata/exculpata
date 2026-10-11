# Temporary-matter retention and expired-backup recovery

R5/F3 of [independent review issue #109](https://github.com/Exculpata/exculpata/issues/109).
This packet investigates recovery and concurrent lifecycle operations. It does
not choose a new retention policy or enroll an installation in backup/export.
See [storage and backup](STORAGE_AND_BACKUP.md) for the complete backup boundary.

## Existing policy

Exculpata is a temporary review workspace. The creation form defaults to
30 days, with 7/30/60/90-day choices. The review end is followed by seven days
of export grace. Once that period ends, enabled maintenance may permanently
delete the matter without an additional confirmation. The owner or an
authenticated administrator can change the date while the matter is active;
ordinary members cannot. The date picker offers a rolling horizon of today
plus 365 days, not a maximum lifetime measured from creation. Repeated
extensions can keep a matter for longer than a year. The date is interpreted
at the end of the selected day in America/New_York; the storage validator's
366-day bound accommodates that end-of-day conversion.

Warnings appear in the application starting 14 days before review ends,
through grace and when deletion is due. These are in-app notices, not promised
email or external notifications. Scheduled deletion waits for active work; this
is a deferral until another maintenance pass, not a fresh grace period. Manual
Close matter separately requires owner/admin authority, exact-name confirmation
and permanent-deletion confirmation. Purge preserves originals outside managed
matter storage and retains content-minimized closure/audit records.

Matters without a stored schedule remain unscheduled. Restoring a backup
preserves its stored dates; it does not restart the 30-day period. A restore
can therefore be due immediately. The maintenance coordinator runs its first
pass at application startup, before waiting its configured interval. Increasing
the interval does not provide an inspection window.

## Hold, inspect, then deliberately activate

1. Restore a consistent, stopped complete backup into a new isolated target
   using its matching reviewed application version. Retain the untouched backup.
   The bundled `./install restore` verifies files in a new target and does not
   start an application or activate the restored node. Its PostgreSQL dump
   listing is not a database import test. Complete that additional acceptance
   step in an isolated database if using PostgreSQL.
2. Keep every restored service stopped for the first inspection. Verify the
   managed storage marker, SQLite integrity/foreign keys, source registry
   versions and digests, and the backup's application/migration version. Include
   `0035_matter_context_selection` and `0036_answer_context`: selections,
   entries, submitted answer snapshots and request outcomes must accompany the
   saved work and original source units. Do not open an upgraded store with an
   older application to test rollback.
3. Before any inspection application starts, set
   `CASE_INTELLIGENCE_MAINTENANCE_ENABLED=0` and
   `CASE_INTELLIGENCE_AUTOMATIC_DISCOVERY=0` in the **isolated target's effective
   app environment**. For Compose this is the target's `config/recordbench.env`
   loaded by the app service; a shell export alone does not override `env_file`.
   Recreate any already-created isolated app container to apply the hold;
   a plain Compose restart preserves that container's previous environment.
   Inspect only the named setting, avoiding output of unrelated secrets. Remap
   all restored storage, account/configuration and database references to the
   isolated target. Keep the authenticated gateway private and confirm no live
   mounts, live database, service registration or job scheduler is reused.
4. While stopped, inventory retained dates and lifecycle states using SQLite
   read-only mode. For example, against the isolated control database:

   ```sql
   SELECT m.matter_id, l.state, r.expires_at, r.purge_after
   FROM workbench_matter m
   JOIN workbench_matter_lifecycle l USING (matter_id)
   LEFT JOIN workbench_matter_retention r USING (matter_id)
   ORDER BY r.purge_after, m.matter_id;
   ```

   This output is private recovery material. Do not publish it. Compare UTC
   `purge_after` to the actual recovery clock. Also inspect durable queues:
   disabling maintenance does not disable worker recovery or make startup
   read-only. If interrupted work exists, remain stopped until its restart
   behavior has been reviewed for this isolated target.
5. Start only the prepared isolated application for authenticated inspection.
   Confirm `/health` reports `maintenance.enabled: false` before using it.
   Check original navigation, exact source versions/digests, saved context and
   historical request receipts, and a complete work-product export. Exports are
   review artifacts, not importable node backups. Do not call
   `run_maintenance_once` or `purge_due_matters` during inspection: those explicit
   entry points still perform maintenance even when the coordinator is disabled.
6. An authorized owner/admin must explicitly decide whether to extend a due
   matter using the normal date-setting UI, keep maintenance held for further
   recovery, or allow the stored deletion policy to proceed. Do not silently
   rewrite dates in SQL. Verify any chosen future date in the saved schedule and
   confirm the matter is still active. A deletion already claimed cannot be
   undone by a late extension. A `purge_failed` matter uses the existing Close
   matter recovery path; it is not automatically reopened.
7. Only after inspection and the operator's activation decision, restore the
   intended maintenance setting and recreate the isolated app container (or
   restart a native app with the new effective environment). Verify `/health`
   reports `maintenance.enabled: true`. Expect an
   immediate first pass: unchanged due schedules can be purged. Check lifecycle,
   content-minimized audit and retained/removed state. Preserve the untouched
   backup until the separately approved recovery/retention process completes.

This procedure describes isolated recovery. It does not authorize changing an
existing installation or taking the restored copy live. Disabling maintenance
also suspends abandoned-upload cleanup, so the hold needs an operator owner and
an explicit end decision.

## Remaining product decisions

| Choice | Consequence to decide before implementation |
| --- | --- |
| Keep temporary workspace with stronger warnings | Choose delivery channels, recipients, failed-delivery behavior and whether silence can permit deletion. In-app notices alone do not prove receipt. |
| Make automatic expiry opt-in | Choose the new-matter default, who may enable it and how existing scheduled matters transition without surprise. |
| Support ongoing case work without automatic expiry | Choose capacity/administration obligations, explicit closure authority and the treatment of existing deadlines. |
| Require confirmation for scheduled deletion | Choose who confirms, what the confirmation binds to, how long it remains valid, and how changes or active work invalidate it. |

For every choice, specify extension authority, warning/grace rules, upgrade
messaging and treatment of both scheduled and grandfathered matters. No choice
is implemented by this correction. Automatic backup or final export is a
separate decision requiring destination, authorization, encryption, capacity,
failure handling, retention and eventual deletion rules.

## Synthetic validation boundary

Separate control-store connections reproduced a purge claim using a stale
active-work inventory and an extension returning success after another writer
claimed deletion. The correction serializes authority, lifecycle and inventory
reads with their writes using immediate SQLite transactions. Ingestion admission,
plan confirmation, analysis start, media admission and overview retry now use
the same boundary; failed-ingestion retry also requires an active matter in its
update. Answer, investigation and every-source retry recheck membership and an
active lifecycle inside their writer transactions. Failed-close export access
does not authorize resuming those jobs in a quarantined matter. Media admission
previously failed with a raw integrity error from its
existing active-matter trigger; it now rejects through the ordinary lifecycle
check. A completed extension still defeats a stale due scan. A purge that wins
first prevents extension/new admission; active work that wins first defers purge.
Late answer completion cannot recreate a deleted conversation.
These tests exercise independent SQLite connections. They do not qualify
multiple complete application processes sharing process-local source locks or
download leases.

The controlled-clock tests cover warning/grace/due boundaries and repeated
rolling extensions. The recovery regression copies a complete stopped synthetic
runtime, removes the original runtime, restores into clean targets with
maintenance held, inspects source bytes and saved Continuity B/C state, then
deliberately tests both unchanged expiry and owner extension before maintenance.
An enabled-startup case verifies that even a 24-hour maintenance interval does
not delay the first purge. All cases retain an unchanged backup and an unchanged
synthetic original outside managed storage.
The original external review package was not imported; these are independent
reproductions. No source normalization, citation basis, migration or retention
default changes are part of this packet.

The runtime drill uses local SQLite/reference retrieval and a deterministic
synthetic generator transport. It establishes persistence, source navigation
and lifecycle behavior, not model quality, encrypted restic/replacement-host
readiness, PostgreSQL import, or CPU/GPU installed acceptance. The existing
`scripts/context-storage-restore-drill.py --answer-context` separately covers
upgrade and matching-reader rollback. Candidate commands, results and hosted
CI/review disposition are recorded in [PR #111](https://github.com/Exculpata/exculpata/pull/111).
