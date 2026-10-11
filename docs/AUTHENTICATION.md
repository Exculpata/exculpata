# Authentication

Local administrators can use **People** to manage accounts when the explicit
[browser account profile](LOCAL_ACCOUNT_BROWSER.md) is enabled. The same page
explains operator setup when disabled and directs OIDC/Kerberos account changes
to the sign-in provider. A new person signs in once before being added to a
matter through **Case team**.

Exculpata has three deployment modes. All use opaque server-side sessions,
CSRF protection, explicit matter membership, and attributed audit events.

## Explicit startup mode

The app factory, identity service and workbench CLI require an explicit
`CASE_INTELLIGENCE_AUTH_MODE` (or the factory's `auth_mode` argument). Missing,
empty and unknown modes stop startup before the app initializes runtime state.
An explicitly empty argument does not fall back to the environment. Installed
Compose operation sets `EXCULPATA_ALLOW_CONTAINER_BIND=1` (deprecated alias: `RECORDBENCH_ALLOW_CONTAINER_BIND=1`) and accepts only
`local`, `oidc` or `kerberos`, including when manually restarted on loopback.
The existing secure-cookie and provider configuration checks still apply.

For deliberate synthetic development only, use:

```console
CASE_INTELLIGENCE_AUTH_MODE=preview recordbench-workbench --host 127.0.0.1
```

`test` is also an explicit isolated test mode. Both development modes deny
non-loopback HTTP clients before login, identity selection or matter access;
the in-process TestClient peer remains available to synthetic tests. Do not
reverse-proxy a development listener or expose it to staff. The supported CLI
ignores forwarded peer headers; installed Kerberos identity still requires its
separate authenticated proxy secret and group checks.

Doctor compares saved and effective authentication without adding auth details
to `/health`; see [authentication diagnostics](AUTHENTICATION_DIAGNOSTICS.md).

## Local accounts

Local mode stores only Argon2id password hashes in an owner-only JSON file. The
login page never lists account names, errors do not reveal whether an account
exists, and repeated failures are rate-limited at both gateway and application
layers. At least one enabled administrator is required. Account changes now
refresh in running processes; password, enabled-state and role changes require
affected users to sign in again. Display-name changes retain their sessions.
See the [local-account lifecycle](LOCAL_ACCOUNT_LIFECYCLE.md) for the shared
service, explicit version 1 migration and recovery contract.

Manage accounts inside the tools container; do not edit hashes by hand:

```bash
docker compose --profile tools run --rm --no-deps account-admin \
  accounts list --file /run/recordbench-secrets/local-accounts.json
```

Use the same `accounts` CLI for `add`, `password`, `display-name`, `enable`,
`disable`, and `role --role reviewer|administrator`, with `--file` and
`--username`. Creation also takes `--display-name`; passwords come from a
terminal prompt or `--password-stdin`, never a command argument. `accounts
migrate --file ... --backup-file ...` prepares existing version 1 files for
changes while retaining a verified recovery copy. CLI output attributes each
completed mutation to its effective service-account UID.

## OIDC

OIDC uses authorization code flow, PKCE, nonce/state, exact HTTPS issuer and
callback validation, signed ID tokens, a file-backed client secret, optional
group admission, and a separate administrator-group mapping. Administrator
roles are sealed into the short-lived server session; provider group changes
take effect at the next sign-in or session expiry. Register the final external origin plus
`/auth/oidc/callback`. Keep provider CA material and client secrets outside Git.

Automation can supply `--oidc-issuer`, `--oidc-client-id`,
`--oidc-allowed-groups`, and `--oidc-admin-groups`. Supply the client secret only
through an owner-readable file with `--oidc-client-secret-file`; never place it
in a command argument, environment example, or repository file.

## Kerberos

Kerberos uses an Apache `mod_auth_gssapi` proxy with an HTTP service keytab. The
gateway clears spoofable identity headers; Apache validates Negotiate, removes
the browser Authorization header, and adds the principal plus a shared secret.
The app revalidates the trusted boundary and resolves allowed/administrator
groups through the joined host’s SSSD NSS socket on every request.

Create the SPN/keytab for `HTTP/<recordbench-host>@<REALM>`, verify there is only
one SPN owner, use AES keys, protect the keytab at `0600`, and rotate it through
a planned outage. Browser integrated-auth policy and DNS must use the same final
hostname. Logging out closes Exculpata’s session; it cannot sign Windows out.

Automation can supply `--kerberos-realm`, `--kerberos-allowed-groups`,
`--kerberos-admin-groups`, and the owner-readable exported keytab through
`--kerberos-keytab`. The installer copies the keytab into its owner-only secret
boundary and never prints its contents.

Never expose the app or Kerberos proxy directly. Only the HTTPS gateway should
publish a host port.

## Application team groups

[Reusable team groups](TEAM_GROUPS.md) documents explicit matter grants, live
revocation, provider boundaries, migration 0031 and backup/rollback constraints.

## Compatible HTTP headers

Trusted proxies may send `X-Exculpata-Authenticated-User` and
`X-Exculpata-Proxy-Secret`, or their existing `X-RecordBench-*` aliases.
Either identity spelling still requires the configured proxy secret. Each
field may use either family independently. Equal copies are accepted; differing
identity or secret copies return HTTP 401 before login or session processing,
without revoking an existing valid session. Empty or incorrect proof never
establishes proxy authority.

Every gateway location strips both families of identity, proxy-secret and
operator diagnostic headers. The Kerberos proxy also unsets both families early,
then sets both identity names from authenticated `REMOTE_USER` and both secret
names from its protected secret file. Do not forward browser-supplied copies or
expose either internal service directly.

The loopback-only operator diagnostic accepts `X-Exculpata-Auth-Diagnostic`
and the deprecated `X-RecordBench-Auth-Diagnostic`. Missing, invalid or conflicting
proof retains its HTTP 404 response; valid proof cannot bypass the peer check.

Export, Playback and Theme responses emit equal values under both
`X-Exculpata-*` and `X-RecordBench-*`. Incoming copies of these response headers
do not control the emitted values. Existing clients can continue reading the
old names while new clients adopt the Exculpata names.
