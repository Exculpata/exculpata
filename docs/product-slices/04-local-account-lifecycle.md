# 04: Provide a shared local-account lifecycle service

Status: implemented on `main` via
[#59](https://github.com/Exculpata/exculpata/pull/59)
([#41](https://github.com/Exculpata/exculpata/pull/41) /
[#55](https://github.com/Exculpata/exculpata/pull/55)). See
[local account lifecycle](../LOCAL_ACCOUNT_LIFECYCLE.md). No dependencies.
This precedes browser account editing.

## Finding and outcome

`admin_cli.py` manages an owner-only account JSON file. `LocalAccountSettings`
loads account data into memory. The application mounts the secrets directory
read-only while the tools container can write it. A browser form alone cannot
safely provide working account changes and immediate revocation.

## Small implementation

Define one account repository/service used by CLI and future browser handlers:
create, change display name, change password, enable/disable, and change
administrator role. Specify atomic writes, concurrent update protection,
cross-process refresh, and revocation of sessions after disable, password reset,
or privilege removal. Preserve at least one enabled administrator transactionally.

Choose and document the narrow persistence boundary before exposing mutations.
Do not make the entire secrets directory writable by the web application. If
moving account data into dedicated storage, preserve existing local accounts
through an explicit migration with backup/clean-restore and rollback evidence.
Maintain the CLI as operator recovery.

## Code and acceptance

Start with `identity.py::LocalAccountSettings`, `IdentityService`,
`admin_cli.py::_accounts_*`, session storage, `compose.yaml`, and
`docs/AUTHENTICATION.md`. Read architecture/security/runbooks before deployment
changes.

Test independent writers, failed writes, restart, account changes observed by
running processes, old-session denial, last-administrator races, and unchanged
OIDC/Kerberos boundaries. Logs/audit record action attribution without passwords
or password hashes. No email service is required.
