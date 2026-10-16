# Local account lifecycle

Local-account changes use one owner-authorized repository shared by the operator
CLI and authenticated browser handlers. See [Browser account management](LOCAL_ACCOUNT_BROWSER.md)
for the optional dedicated account-directory profile and first-use People flow.

## Persistence and authority

By default, keep `local-accounts.json` in the existing secrets directory, owned by the
application service account with mode `0600`. Its directory must belong to that
account and cannot be writable by other users. Every ancestor is opened without
following symlinks and must be owned by root or the service account, without
group/other write permissions. Only root-owned mode-1777 intermediate temporary
directories are permitted as a sticky-directory exception; the final account
directory never uses that exception. Reads (including cache hits), writes,
migration recovery and relocation enforce the same traversal rule before
creating children. Use a local POSIX filesystem
with reliable advisory locks, atomic rename and durable file/directory sync.
The web application's secrets mount remains read-only. Only the operator's
existing tools container can mutate accounts. The optional browser profile
explicitly relocates that same canonical file into a dedicated writable account
directory; the original secrets mount remains read-only. It adds no proxy
exception, default account or authentication bypass.

`LocalAccountRepository` provides initialize, create, display-name change,
password change, enable/disable and administrator-role change operations.
The caller supplies an action attribution; the CLI identifies its effective
service-account UID and prints a content-minimized completion receipt. Receipts
contain action, account name and actor, never passwords, hashes or session
revisions. The browser caller must first authorize an administrator,
validate CSRF and record its authenticated principal in the existing audit
system. Possessing a repository object does not supply that authorization.

Display names use the same NFC normalization and Unicode character policy as
workspace principals. Create and rename reject control and format characters
before writing the account file; supported accented, non-Latin and emoji names
remain usable at sign-in and during session refresh. The 160-character limit
applies after normalization and trimming.

Each writer holds a persistent owner-only sibling lock file across a fresh
read, validation, mutation and atomic replacement. The last enabled
administrator check shares that lock, so independent writers cannot both
remove the last administrator or overwrite each other's changes. Writers
validate the complete candidate before publishing it, sync the temporary file,
replace it and sync the directory. Never remove a live lock file, edit the JSON
by hand or mix this service with older writer tools.

Readers open bounded regular files through directory descriptors without
following symlinks. They do not create locks or write probes. Each login and
session resolution reopens the path and checks directory ownership plus file
type, owner, mode and size. A process retains one immutable validated snapshot
keyed by directory/file identity, size, nanosecond modification/change times
and the bounded file contents. A timestamp collision cannot hide changed bytes.
Unchanged files avoid JSON parsing and validation of all accounts on every
protected request. Atomic replacement or metadata/content changes trigger a
bounded reload; concurrent in-place changes detected during a read are refused.
Readers never cache failures or fall back to an older snapshot. Missing, unsafe
or malformed files deny access and clear the cache. Changes therefore reach
running processes without restart, including those made by another process.
A write failure before replacement preserves the old account file; a directory
sync error after replacement leaves the commit's durability uncertain. Inspect
account state before retrying an operation after a storage error.

## Session behavior

Version 2 adds a random revision to each account. Password changes, enable/
disable operations and role changes rotate that account's revision. Existing
sessions are denied on their next request, even if an account was disabled and
re-enabled, or demoted and promoted, before that request. Users sign in again
to receive the new state. Display-name changes refresh visible identity without
ending sessions. Unrelated accounts retain their sessions.

The opaque local session cookie carries a keyed binding to the account's
revision and credential/role state; it exposes no password hash or stored
revision. SQLite still stores only the token digest in its existing session
schema. Sessions issued by older releases require sign-in again after this
upgrade. A request already authorized before an account change is not cancelled
mid-flight. OIDC and Kerberos retain their existing provider and trusted-proxy
boundaries.

## Existing installations: explicit migration and recovery

Version 1 files remain readable for sign-in and listing. Mutation refuses them
until the operator makes an explicit verified recovery copy:

```bash
exculpata accounts migrate \
  --file /srv/exculpata/secrets/local-accounts.json \
  --backup-file /mnt/recovery/exculpata/accounts-before-v2.json
```

Run this in the approved operator environment with the correct mounted paths.
The destination must be new and protected. Migration copies the exact version 1
bytes, syncs them, reads them back and validates their account data before
atomically replacing the live file with version 2. It preserves account names,
display names, Argon2id hashes, enabled flags and administrator roles. Migration
ends existing local sessions at their next request. Newly initialized nodes use
version 2 directly.

Back up accounts with the existing configuration, control database and session
key boundary in [Storage and backup](STORAGE_AND_BACKUP.md). Stop all operator
account writers during a consistent snapshot, including the tools container;
stopping only the web application does not stop separate CLI processes.

A clean restore must place the account file in a new owner-controlled directory,
restore mode `0600`, start the matching reviewed release, and verify synthetic
sign-in plus administrator recovery.

To roll back to a release that understands only version 1, stop the application
and **all account writers**, including separate tools containers. Preserve a
consistent current backup, then use this release's operator command while the
node remains stopped:

```bash
exculpata accounts rollback \
  --file /srv/exculpata/secrets/local-accounts.json \
  --backup-file /mnt/recovery/exculpata/accounts-before-v2.json \
  --workspace-file /srv/exculpata/runtime/workbench.sqlite \
  --confirm-stopped
```

Use the installation's actual canonical account path and control database path
in the operator environment. `--confirm-stopped` is the operator's confirmation;
it does not stop processes. If recovering the control database too, restore it
first and run the command against the exact database the old release will use.
Do not replace that database with an older copy after session invalidation.

The command holds the account writer lock, validates the protected v1 recovery
copy and existing workspace, then commits invalidation of every local-authentication
session before atomically restoring the exact v1 account bytes with mode `0600`.
It neither initializes nor migrates the database. It replaces each local
session's cookie lookup digest and marks the row revoked, preserving existing
revocation timestamps, session IDs and append-only audit links. Non-local
sessions, principals, matters and audit events remain unchanged. The recovery
artifact and session key remain unchanged. A missing, unsafe or incompatible
database or failed session invalidation
prevents account replacement. If account replacement fails after invalidation,
local sessions remain revoked: keep writers stopped, inspect the account file
and retry the command. A completed rollback can also be repeated safely.

Restart the compatible previous release and require fresh password sign-in;
verify administrator recovery before opening access. Raw-copying the v1 backup
can revive an old cookie with its original workspace and key if that cookie was
never presented against v2. Exact v1 artifacts have no revision field to rotate,
so migration's live revision changes alone do not make that rollback safe. Later
account changes are absent from the old copy; reconcile them deliberately.
Never strip revision fields from the live file to force an older reader to load
it. Keep the migration recovery copy until the rollback window closes.

Synthetic regression tests cover process writers, last-admin races, failed
writes, concurrent readers, fresh process snapshots, revocation and non-revival,
backup verification, clean restore and exact version 1 rollback with both old
and migrated cookies denied without intervening requests. Failure tests cover
session-invalidation ordering and account-write recovery. A near-limit
500-account fixture verifies that repeated session resolutions parse only once;
replacement, metadata changes and invalid-file tests verify cache invalidation.
These tests do not replace an operator's installed-node backup and recovery drill.
