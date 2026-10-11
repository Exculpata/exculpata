# Reusable team groups

Administrators open **Team groups** from the account menu to create a named
Exculpata group and add or remove people who have signed in. Group names are
unique without regard to ASCII case. Groups do not nest and do not synchronize
with OIDC or Kerberos directories. Those providers continue to govern admission
and administrator roles; their groups never become matter grants automatically.

On a matter's **Case team** page, its owner or an authenticated administrator
can explicitly grant or remove group access. Each person appears once, with all
current reasons: **Owner**, **Direct member**, and **Group: name**. A group member
can receive an independent direct grant. Removing a direct grant, a person from
a group, or a group grant removes only that authority. Other grants continue;
removing the last grant denies access. Groups never confer ownership or system
administration. Administrators retain existing, explicitly labelled oversight;
a system role does not create membership.

## Live revocation

The control database joins group membership to explicit matter grants on every
authorization check. Direct and group grants share one effective-membership view,
including SQL checks used by queued answers, research, review and report
compilation, and their completion fences. Group writes use immediate SQLite
transactions; no per-session grant cache or copied direct membership is created.
Open sessions lose access on their next request. Bytes already delivered to a
browser or downloaded outside Exculpata cannot be recalled.

Local account existence and enabled state are checked against the canonical
account file from the data layer, including workers without a browser request.
Missing, unsafe or unreadable account state denies local access. Independent
report lease connections install the same eligibility function. Provider
admission/role lifetimes remain those in [Authentication](AUTHENTICATION.md):
OIDC changes take effect at sign-in/expiry, Kerberos at its existing fresh checks.
Disabling a stored principal also denies matter access. Re-enabling a principal
can restore its retained grants; remove grants as well for permanent revocation.
Case team exposes retained direct grants without active access so an owner or
administrator can remove them before an account is enabled again.

Work can finish an already dispatched model call, but its result cannot be saved
through the authorized completion fence after revocation. Transcription rechecks
before dispatch, polling and transcript import. Existing source preparation and
automatic transcript overviews remain matter-owned processing of admitted
sources, rather than a grant to the original uploader. Revocation does not delete
sources, citations, human decisions or previously saved work. Retrying user work
requires current matter access. Transcription Retry and Continue bind the new
attempt to the acting member, so an owner can recover work after the original
requester loses access. Subsequent worker and import checks use that member;
the existing source identity and upload provenance remain unchanged.

## Migration and recovery

Mirrored migration **0031** adds group, group-member and matter-group-grant tables
and the effective-membership view. It changes neither existing direct rows nor
source/model/provenance storage. Grant and member inserts retain actor/time;
transactional audit events record the acting principal, target group/person and
request/session attribution. Group grants are removed with the existing matter
purge boundary; reusable groups remain available for other matters.

Stop application writers before upgrade and retain a verified pre-upgrade backup.
Include these tables in the same control-database backup as direct grants, audit
history and sessions. Restore to a clean target with the matching application and
canonical local-account file. `WorkspaceStore` registers the live eligibility
function required when querying the effective-membership view. Raw SQLite
integrity and foreign-key checks remain available without that function.

**Do not run older writers against the upgraded database.** They cannot interpret
group grants or enforce their revocation. Rollback requires the stopped
pre-upgrade snapshot and matching old application in a clean target. Export any
needed post-upgrade work first; that work is absent from a pre-upgrade snapshot.
Restoring an earlier access snapshot can restore grants; review membership and
invalidate restored sessions before reopening the installation.

## Synthetic acceptance

`tests/test_team_groups.py` exercises two groups/two matters, mixed grants,
last-grant revocation, disabled/removed local accounts without a browser request,
concurrent connections, actor attribution, queued-answer refusal, report leases
and late completion, mirrored migration, SQLite backup and clean restore.

`scripts/qa-team-groups-browser.py` starts a disposable loopback HTTPS application
and runs two actual local sign-ins in independent browser contexts. It creates
two groups and matters, verifies group-only access and matter isolation, checks
all displayed access reasons, removes grants while the reviewer remains signed
in, and captures desktop/mobile layout. The same session can export while authorized
and is denied export after its last grant is removed. Run with the contributor environment,
Node, OpenSSL, and an installed Playwright browser; optional `--artifacts` must
point outside the checkout. This is synthetic acceptance, not an operator's
installed-node or replacement-host recovery certification.

The [slice 06 acceptance record](TEAM_GROUPS_ACCEPTANCE.md) distinguishes native
checks, baseline comparison and pending hosted gates.
