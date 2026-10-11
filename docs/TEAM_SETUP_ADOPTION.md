# Team setup: adoption and recovery

This is a portable development candidate, not an installed-node acceptance
receipt. It builds on the accepted upstream baseline and the repaired installer
and local-account lifecycle foundations. Review and validate the final combined
commit before adopting it. Keep deployment-specific configuration and retained
records outside the public source repository.

## What changes

| Area | Result | Adoption work |
| --- | --- | --- |
| Installer | Read-only prerequisite checks, protected directory creation and resumable setup using saved configuration | Check the existing service identity, storage ancestry and configured hardware plan before invoking resume or update. A newly reported prerequisite failure is a stop to correct the environment, not permission to replace existing state. |
| Local account lifecycle | Current account state reaches existing processes; password, role and enabled-state changes invalidate prior local sessions | Expect users with sessions from older code to sign in again. Existing version 1 account files remain readable; mutation requires explicit migration with a verified recovery copy. |
| Browser account management | Administrators can manage accounts through People when explicitly enabled | Use a dedicated protected canonical account directory and the matching opt-in Compose overlay. Existing installations require a stopped-node relocation and a recovery copy. The general secrets mount remains read-only. |
| Account backups | A frozen canonical account store must be structurally usable before backup or restore can succeed | Capture account writers in the consistent stopped boundary. Verify restoration and synthetic administrator sign-in with the matching application version. |
| Team setup | A first administrator can follow sign-in, matter creation, source preparation and teammate-access steps | Use a synthetic practice matter and separate assigned/unassigned accounts. Progress is derived from current records and access events; it does not certify model quality or complete source review. |
| Model receipts | Cache snapshot paths, link relationships and artifact bytes are checked together | Existing receipts may need regeneration under the new receipt contract. No model revision changes are included. An old blob remaining in cache does not make a missing or changed snapshot link valid. |

## Build a downstream candidate

1. Record the exact upstream commit and the currently deployed application
   revision. Compare the complete code difference, dependencies, configuration
   contracts and any local patches. Resolve product differences in source;
   keep private deployment overlays separate.
2. Build an isolated candidate using the deployment's existing identity mode,
   service account, storage layout and selected model configuration. Do not
   replace those settings with example values from the public repository.
3. Validate the combined source revision: application and transcription tests,
   Compose graphs, focused installer/account regressions, and real browser
   acceptance. Separate a source-test pass from target-host runtime evidence.
4. Before promotion, capture a consistent recovery boundary and restore it into
   an isolated target. Keep the previous application distribution and the
   matching configuration, account file and session-key boundary available.
5. Promote only the exact candidate that passed the deployment's acceptance
   checks. Record its revision, configuration changes, migration steps, test
   results and recovery location in the private deployment record.

## Installed acceptance sequence

Check the read-only preflight and saved configuration first. Verify that an
interrupted installation resumes without replacing sources, account identities,
secrets or verified model artifacts. Distinguish prepared configuration,
running containers, a reachable sign-in page, successful sign-in, usable basic
review and each selected optional capability.

With synthetic accounts, verify administrator access and ordinary-user denial
to account administration. Create a teammate, change the display name while the
teammate is offline, and confirm that case-team views show the current name.
Check stale forms, password reset, disable/re-enable, role changes and their
direction-specific audit entries. Old sessions must not regain access after a
disable/re-enable or demote/promote cycle. Retain a usable administrator during
the exercise.

Create a synthetic matter, add a document and wait for the normal preparation
state. Assign one teammate and keep a second account unassigned. The assigned
account must open the matter and source; the unassigned account must be refused.
Verify saved notes, a representative Report and export through the existing
review workflow. A recorded page open is only access evidence, not proof of
source accuracy or a completed review.

Finally, perform a clean restore and administrator recovery using the matching
application version. Synthetic subprocess fixtures do not establish real
encrypted backup, database import or replacement-host recovery acceptance.

## Rollback boundaries

Account format version 2 is not writable by older version 1 tools. Stop the
application and all account writers, then use the
[operator rollback command](LOCAL_ACCOUNT_LIFECYCLE.md#existing-installations-explicit-migration-and-recovery)
to invalidate local sessions from the final recovery database before restoring the
exact pre-migration copy. Restart the matching previous application and verify
fresh administrator sign-in. A raw file copy can revive an unresolved old cookie.
Later account changes are absent and must be reconciled deliberately. Do not
strip revision fields or allow older and newer writer tools to run together.

Browser account relocation changes the canonical account path. A rollback must
restore the matching mount configuration and account-file location; reverting
source code alone is insufficient. Keep recovery files outside the canonical
account directory and preserve their protected ancestors.

See [local account lifecycle](LOCAL_ACCOUNT_LIFECYCLE.md),
[browser account management](LOCAL_ACCOUNT_BROWSER.md),
[first run](FIRST_RUN.md), [installation](INSTALL.md), and
[storage, backup and recovery](STORAGE_AND_BACKUP.md) for the corresponding
operator contracts. Adoption notes should travel with the tested candidate;
upstream merges do not replace an operator's promotion decision.

The People setup page directs operators to the recovery documents bundled with
the reviewed release currently running on their node. Retained account lock and
temporary files must have service ownership and mode 0600, like the canonical
account file; saved-node preflight refuses mismatched restore metadata. The
synthetic People browser runner discards inherited Exculpata deployment
settings before importing the application, retaining only its QA browser options.
