# Storage, backup, and recovery

Matter storage must be a dedicated absolute directory initialized with a
Exculpata ownership marker. The application will not delete outside that
boundary. A local filesystem or host-mounted NAS path is supported; the
installer does not mount network shares or store NAS credentials.

Both `.recordbench-managed-storage.json` and `.exculpata-managed-storage.json`
are supported, with each generation's original product/storage-ID validation.
Opening or restoring a marker never rewrites it. Conflicting markers are refused.
Fresh installs use the Exculpata marker; existing nodes retain the legacy marker.

Capacity policy separately limits a matter, an upload collection, media files,
documents, and the protected free-space reserve. Thousands of sources are
represented through paginated catalogs, background jobs, readiness counts, and
partial-query behavior. One failed source is excluded and reported; it does not
make every other indexed source unusable.

## Choose your backup method

Operators can use their existing backup system or the optional bundled restic
tool. Installing Exculpata does not enroll the host in a backup service.
The bundled tool takes an operator-selected absolute destination on a filesystem
available to the host, including storage mounted by the operating system.
Exculpata does not mount shares, collect storage credentials, or require a
particular storage vendor. Configure encryption, access, retention and recovery
for the chosen method and test it before depending on it.

Whichever method you choose, capture these as one consistent boundary:

- managed matter storage;
- the control SQLite database and session key;
- generated configuration and secrets;
- the dedicated canonical account directory when browser account management is enabled;
- the PostgreSQL projection dump (or a separately tested projection rebuild);
- version/install metadata and integrity hashes.

The transient transcription queue, model cache, container images, and other
rebuildable caches are excluded. Keep the matching reviewed release distribution
available too; the bundled snapshot records installation/version metadata,
not a copy of every release capsule or container image.

An external backup system must stop application writers or provide an
equivalent tested consistent snapshot across the control database, managed
registries/storage and projection. A file copy of a running SQLite database
is insufficient, especially when its journal/WAL is separate. Test recovery in
an isolated target, including registry integrity and matching source versions.
For updates using a verified external backup, `./install update --no-backup`
skips the bundled pre-update backup requirement; it does not verify that
external backup. This flag does not bypass a configured bundled backup's
failed or deferred result.

## Optional bundled backup

The bundled method uses encrypted restic storage with the recovery password
copied to a separate protected location. The repository and recovery credential
must not live inside the node or inside each other. Scheduling is opt-in.

The examples below are for new Exculpata nodes. Existing nodes must keep their
original root, service account, repository and recovery-key locations. Backups
retain the `recordbench` restic tag and `recordbench-backup-<hash>` units on legacy
nodes. New nodes use `exculpata` and `exculpata-backup-<hash>`. Scheduling never
replaces legacy units with new names. Status and restore accept either restic tag;
retention remains scoped to the node's existing generation. PostgreSQL dumps and
imports continue to use the `recordbench` user/database.

`scripts/exculpata-backup.sh`, `scripts/exculpata-backup-status.sh`,
`scripts/exculpata-restore-drill.sh` and `scripts/exculpata_backup.py` expose the
same operations as the retained `recordbench` entry points. Either spelling can
read either generation. A verified restore preserves the archived config and
marker bytes; it does not perform a naming migration.

Install `restic` on the host first; the bundled tool invokes it from the service
account's PATH. Use the distribution package when available. If its mirror fails,
the supported alternative is the upstream precompiled binary from
[official restic releases](https://github.com/restic/restic/releases), following
[upstream installation and verification](https://restic.readthedocs.io/en/stable/020_installation.html).
Choose an explicit version and the host OS/architecture asset, verify its
compressed-file SHA-256 against that release's signed `SHA256SUMS` (verify the
signature with the upstream signing key), then decompress and have the
administrator install the executable in `/usr/local/bin` with mode 0755.
Record the version and verify `restic version` in a fresh service-account session.
Do not substitute an unsigned mirror or pipe a download into a shell. Scheduling
also needs a PATH containing that binary. This fallback does not install systemd
or enable scheduling on a host without a working service manager.

Have an administrator precreate dedicated service-owned repository/recovery
parents when their mount or `/srv` parent is root-owned. Keep the recovery key
separate from the repository; the existing local-backup opt-in still applies.

Initialize and run a backup with:

```bash
./install backup --root /srv/exculpata \
  --repository /mnt/backup/exculpata/restic \
  --recovery-key-output /mnt/recovery/exculpata-restic-password \
  --schedule-backups
```

The backup checks every durable queue. If work is active, it records a deferred
state and the quiet-hour timer retries. Otherwise it stops the app and gateway,
then dumps PostgreSQL and creates hard-link snapshots beside the node state and
managed matter storage—even when those roots are on different filesystems—and
restarts service before encrypted transfer. Every SQLite store in both snapshot
trees is copied with its journal companions, consolidated through SQLite's
backup API, checked, and detached before restart. Live database or WAL writes
after restart therefore cannot change the frozen copy. Symbolic links and
unsupported file types in either snapshot tree cause a failed backup.

The PostgreSQL dump belongs to the same stopped application boundary as the
source/control stores. The app remains stopped during the dump and SQLite
consolidation; allow for that time in the maintenance window. Failure during
capture attempts to restart the app and gateway and removes temporary snapshot
trees. The tool retains its normal locking, active-job deferral, encryption and
retention behavior.

The tool verifies all SQLite stores, control hashes and the PostgreSQL dump
listing. Its routine repository check reads the fixed `1/14` subset; it does
not rotate automatically. Use `restic check --read-data` with your configured
repository/credential for a full data read when required by your verification
policy.

Precreate the restore-drill **parent** as administrator; the service account
cannot create new top-level directories in root-owned `/srv`:

```bash
sudo install -d -m 0700 -o exculpata -g exculpata /srv/exculpata-drills
```

Keep that parent outside the live node, matter storage and backup repository.
Leave each child target absent: restore deliberately refuses existing targets.
As the service account, check status and perform the actual recovery test:

```bash
./install backup --root /srv/exculpata
./install restore --root /srv/exculpata \
  --restore-target /srv/exculpata-drills/restore-drill-001
```

Restore refuses an existing target and never connects drill data to the live
node. It asks restic to verify restored files, checks both snapshot trees,
requires the managed-storage marker, and validates every control and managed
SQLite store. New snapshots record SQLite inventory counts and restore checks
those counts; older snapshots without counts still receive all-store checks.
The receipt reports total and managed SQLite stores separately. The routine
drill lists the PostgreSQL dump; an actual import into a disposable database is
an additional recovery acceptance step. Inspect the receipt, then remove the
exact drill directory yourself; it contains decrypted confidential state.
Never declare backup ready from a successful upload alone.

Restored matter deadlines keep their original dates. Before starting a restored
application, follow [expired-matter recovery](EXPIRED_MATTER_RECOVERY.md): hold
maintenance in the isolated target's effective environment, inspect while held,
and decide each due matter's disposition before activation. Enabled maintenance
runs immediately on startup; its interval is not an inspection grace period.

## Synthetic consistency verification

The regression suite simulates writes completing at stop and new writes after
restart. It checks DELETE-journal registries, WAL control databases, matching
projection capture, failed/corrupt/missing stores, symlink refusal, cleanup and
unchanged active-work deferral. To additionally exercise a real encrypted
repository and a real PostgreSQL dump/import using only synthetic state:

```bash
# Requires restic, Docker and an already installed pgvector/pgvector:pg17 image.
RECORDBENCH_BACKUP_INTEGRATION=1 python -m pytest tests/test_backup_consistency.py -q
```

This opt-in test uses an image already on the host, a disposable database with
no network, no published port or live volume, and a temporary repository. It
checks a full repository data read, restored SQLite values, PostgreSQL import
and `pg_amcheck`. The database container is removed even on failure. Test files
are synthetic pytest temporary data. It simulates app stop/start; it does not
replace an operator's installed-node or replacement-host acceptance.

The tooling remains prerelease. Do not depend on this candidate as the sole copy
of retained work product until your own scheduled backup and restore drill pass.

## Browser account snapshot validation

When the dedicated browser-account profile is enabled, backup requires its
canonical `local-accounts.json` in the frozen copy before application restart,
transfer or retention. The copied directory must be owner-only `0700` and its
account/lock/temp files regular owner-only `0600` files. The canonical file is
bounded to 1 MiB and must contain a valid version 2 store with an enabled
administrator. Missing files, malformed JSON, invalid account fields, unusable
Argon2id encodings or permissions fail the snapshot; the application restart
recovery still runs. Restore repeats this check against the restored installation
record as well as checking file hashes, so checksums alone do not establish a
usable account format.

Runtime and backup share a standard-library format validator for account shape,
PHC Argon2id encoding, parameter ranges and revision fields. The backup tool runs
this exact bundled validator in isolated Python, without relying on host Argon2
packages or ambient Python imports. Runtime readers retain their additional
argon2-cffi parameter validation and password authentication. Structural backup
validation cannot establish that an operator knows a working administrator
password; the clean restore drill must still verify synthetic sign-in and
administrator recovery. The suite covers a clean account restore, corrupted
frozen copies, invalid modes, missing administrators and malformed hashes.


## Full-text review ledgers

Include additive full-text tables, resource receipts and counters in the control
SQLite backup. Online backup/clean-restore regression coverage checks saved
partial outcomes and foreign keys. Logical ledger charges are independent of the
SQLite/WAL file size, protected disk reserve and backup-retention policy. Stop
workers before upgrading older full-text writers; legacy ledgers are retained
and charged but require a newly admitted run to resume analysis. Export before
creator-only terminal deletion if findings or human decisions must be preserved.
See [Full-text review](FULL_TEXT_REVIEW.md) for limits and rollback boundaries.

## Report workflow migration and recovery

Queued and running report compilations appear in content-free health counts and
defer backup before services are stopped, alongside other active background work.


Report workflow migrations 0027 and 0029 are additive: they introduce a durable
compilation-request table and the `compilation_basis` section column. A current
backup and clean restore retain request leases, fingerprints, Report text, and
separately stored provenance. The restored coordinator fences expired workers
before retrying work; active requests still prevent an operator backup.

Before upgrading, retain a verified backup of the stopped prior revision. Rollback
to a revision without these migrations uses that pre-upgrade backup restored into
a clean target and the matching old application revision. Do not run old code
against the upgraded database: old section readers do not recognize the added
column, and old cleanup does not own the new request table. Keep any post-upgrade
work separately before restoring; a pre-upgrade snapshot does not include it.

The synthetic `scripts/report-storage-restore-drill.py` creates a report using the
public pre-workflow baseline, takes a SQLite online backup, upgrades a separate
working copy, then restores the snapshot to a clean target and reads it with that
original baseline code. It checks report content and SQLite integrity in both
states. Run with the contributor Python environment and optional
`--baseline-ref <pre-workflow-commit>`. The default baseline is
`1fed77a2c50686b073e4c3255421d77aa16fefe5`. It uses temporary synthetic data and
does not inspect an installed node. This complements the focused queue and
provenance backup/clean-restore tests; it does not establish an operator's own
backup or replacement-host readiness.

## Local account format rollback

A v1 migration recovery artifact preserves exact old bytes, which can restore
an old cookie's account binding. After any control-database recovery, keep the
application and all operator account writers stopped and run the
[account rollback command](LOCAL_ACCOUNT_LIFECYCLE.md#existing-installations-explicit-migration-and-recovery)
against the database the previous release will actually use. It commits local
session invalidation before restoring the v1 file, without database initialization or
migration. Preserve the original recovery artifact; require fresh sign-in and
administrator recovery after restart. Never overwrite the invalidated session database with
an earlier snapshot before opening access.

## Application team groups

[Reusable team groups](TEAM_GROUPS.md) documents explicit matter grants, live
revocation, provider boundaries, migration 0031 and backup/rollback constraints.

## Matter entities

[Entity workspace](ENTITY_WORKSPACE.md) documents additive migration 0032,
entity/mention/history retention, final-bundle preservation and clean restore.
Rollback requires the pre-upgrade backup and matching old reader; old purge
code must not operate on upgraded entity state.

## Events and assertions

[Events and assertions](EVIDENCE_ASSERTIONS.md) adds migration 0034 to the same
control backup boundary. Preserve original source bytes separately from exported
work product. Clean restore verifies originals, explicit roles, conflicting
accounts and decision history. Rollback requires the verified pre-upgrade backup
and matching older reader; older purge code must never open upgraded state.

## Single-run full-text synthesis receipts

The [adapter](FULL_TEXT_SYNTHESIS.md) adds versioned plan/result JSON to the
existing research-job table; it adds no SQL schema or new runtime file. A complete
stopped-runtime backup must still include the control database, matter catalog,
source originals, parsed units and other normal runtime stores. Active synthesis
is existing research work and blocks closure/backup through those guards.

The [synthetic clean-restore receipt](FULL_TEXT_SYNTHESIS_ACCEPTANCE_2026-09-12.md)
checks complete copied runtime files with the original paths unavailable,
original navigation, two reused nodes, interrupted-request accounting, completion
and Word/Markdown/JSON exports. Rollback uses the exact pre-upgrade backup in a
clean target with its matching old revision. Do not run old writers against new
job JSON: they do not understand this adapter or its saved charges. Keep the
upgraded backup and post-upgrade exports separately.

## Reviewer context selections

Continuity B adds paired migration `0035_matter_context_selection.sql` to the
control database. Include `workbench_context_selection` and
`workbench_context_entry` in the same stopped, complete-runtime backup as the
shared notes, entities, assertions and original sources. Entries contain typed
references, approved-version/dependency fingerprints, ordering and attribution;
they are not copies of current facts and are not historical answer inputs.
A complete matter purge removes both tables' matter state. Complete work-product
exports include `context/selections.json`; note-only exports remain note-only.
Exported manifests are not restore archives or current-support attestations.

Stop all application writers before upgrading. Mixed old/new writers and using
old purge code on the upgraded database are unsupported. Rollback uses the
verified pre-upgrade backup and its matching reader, losing subsequent B changes
unless separately retained. `scripts/context-storage-restore-drill.py` exercises
baseline migration, retained legacy answer notebook scopes, clean restore and
matching-reader rollback with synthetic state. The HTTP acceptance additionally
restores a stopped complete runtime and follows original-source links before
export and purge. See the [bounded brief](product-slices/continuity-b-context-selection.md)
for executed evidence and outstanding acceptance.

## Focused-answer context receipts

Continuity C adds paired migration `0036_answer_context.sql`: immutable submitted
selection snapshots and per-runtime-request manifests/outcomes, cascading with
the existing answer job and matter purge. Include them in the same complete,
stopped control/runtime backup. Tokenization, generation, repair, failed and
cancelled attempts are retained; restart never substitutes the current selection.
Knowledge deletion does not erase historical submitted/dispatched text. Matter
purge removes it. Sixteen recorded runtime requests per answer (including
preparation/tokenization) bound retries; each manifest is at most 128 KiB and the
snapshot at most 512 KiB. Complete conversation reads and matter receipt exports
preflight an 8 MiB aggregate bound and refuse excess rather than omit receipts.

`python scripts/context-storage-restore-drill.py --answer-context` upgrades the
accepted B revision, checks clean restore of prepared C receipts and legacy
notebook scopes, and verifies pre-upgrade rollback using the matching B reader.
The synthetic C HTTP regression also restores the entire stopped runtime with
original paths unavailable, reopens original support and exports, then purges.
These do not replace an operator's own encrypted backup/replacement-host drill.
Stop writers before upgrade. Mixed-version writers are unsupported; rollback
uses the verified pre-upgrade complete backup and matching application revision.

For the local-account reference profile, continue from verified files to an
actual isolated PostgreSQL import and application boot using
[isolated node recovery](ISOLATED_NODE_RECOVERY.md). The original node must stay
outside every replacement mount and volume boundary.
