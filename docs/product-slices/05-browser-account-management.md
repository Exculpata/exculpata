# 05: Manage local accounts in the browser

Status: implemented on `main` via
[#59](https://github.com/Exculpata/exculpata/pull/59)
([#43](https://github.com/Exculpata/exculpata/pull/43) /
[#57](https://github.com/Exculpata/exculpata/pull/57)). See
[browser account management](../LOCAL_ACCOUNT_BROWSER.md). Depends on 04.

## Finding and outcome

The browser lists known principals and can assign an existing principal to a
matter. `docs/AUTHENTICATION.md` sends local account management to a container
command. A local administrator should create a teammate and manage access
without returning to a shell.

## Small implementation

Add an administrator-only local Users page over the shared service from 04:
create an account, edit its display name, set/reset a password, disable/re-enable
it, and grant/revoke administrator role. Clearly separate application role
from case-team membership. Use safe direct password entry for the first version;
email invitations are a separate capability.

Replace hard-coded domain/AD language with mode-appropriate controls. For
provider-managed identities, show where accounts are managed and retain normal
matter membership UI. Do not pretend local account forms manage the provider.

## Code and acceptance

Start with administrator routes in `workbench.py`,
`templates/workbench_admin.html`, `identity.py`, and identity/membership tests.
Add help to `docs/AUTHENTICATION.md` and connect onboarding from 03.

Browser acceptance: an administrator creates two synthetic users, each signs
in, only the authorized user sees a restricted matter, password reset revokes
old access as specified in 04, and disable works with an already-open session.
Check CSRF, ordinary-user denial, concurrent-edit recovery, last-administrator
protection, keyboard use, and narrow layouts. Never return credentials or
hashes in account listings or general audit events.

No separate account store or deployment-specific implementation is allowed.
