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
CASE_INTELLIGENCE_AUTH_MODE=preview exculpata-workbench --host 127.0.0.1
```

`test` is also an explicit isolated test mode. Both development modes deny
non-loopback HTTP clients before login, identity selection or matter access;
the in-process TestClient peer remains available to synthetic tests. Do not
reverse-proxy a development listener or expose it to staff. The supported CLI
keeps the direct TCP peer (`proxy_headers=False`); installed Kerberos identity
still requires its separate authenticated proxy secret and group checks.

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

For local sign-in throttling, both gateway locations overwrite `X-Forwarded-For`
with `$remote_addr`. The app accepts exactly one header containing a single IP
literal, without lists, ports, whitespace or IPv6 scope IDs, only from a trusted
direct peer. With `EXCULPATA_ALLOW_CONTAINER_BIND=1` (or its deprecated alias),
trusted peers are the exact IPs resolved for Compose service `gateway` on each
login attempt. The `/auth/local` handler is synchronous, so FastAPI runs it,
including DNS resolution and password verification, in its worker thread pool
rather than on the asyncio event loop. This relies on Compose's
operator-controlled DNS and private app listener; only the gateway publishes a
port. No private subnet is trusted.
Other deployments trust no peers by default; the app factory's
`local_gateway_peers` tuple can explicitly supply gateway IP literals, and an
empty tuple disables header trust. Missing, duplicate or malformed headers,
untrusted peers and failed resolution fall back to the direct peer address
(`unknown` if absent). Browser-supplied forwarded prefixes never select a key.

Each app process allows at most 10 failed verifications per normalized username
and client, 50 per client across all usernames, and 100 per normalized username
across clients, in a rolling 15-minute window. Pending verifications reserve
capacity against all three limits under a short lock. Password verification runs
outside that lock. Pending reservations do not expire while verification is
running; completed failures enter the window at completion. Errors release their
reservations without recording a password failure. Throttled requests do not
verify passwords, add failures or extend the window. Successful sign-in clears
completed failures only for that username and client, preserving other pending
attempts, the client's cross-username budget and the username aggregate.

Memory is bounded without refusing sign-in because a key table is full:

- At most 1,000 client records are retained. Each has at most 50 username records
  and 50 failed or pending attempts across those usernames. Existing clients
  cannot evict their own records by changing usernames. A new client replaces
  the least recently admitted client without pending checks. If all records have
  pending checks, it waits on a condition that releases the lock until a check
  completes, rather than failing due to table capacity.
- Username aggregates use 10,000 fixed buckets selected by a keyed digest of the
  normalized username, each with at most 100 failed or pending attempts. They
  survive client eviction and successful sign-in. Bucket collisions share a
  budget, so they can throttle earlier, but cannot increase a username's allowed
  guesses. A single client's 50-attempt budget cannot fill even one bucket.
- Together these structures retain at most 61,000 window records and 1,100,000
  failure timestamp references, plus bounded pending counters. Idle expired
  entries remain within these bounds and are pruned when accessed. State resets
  on process restart.

This trades exact independent username budgets and indefinite client retention
for bounded memory and fresh-client admission. Client churn from multiple
addresses can evict another client's throttle; it cannot erase the username
aggregate. Distributed failures can fill an aggregate bucket and temporarily
block even correct passwords for its usernames. Collisions can also affect an
unrelated username. A single address cycling usernames cannot evict its own
active throttle or fill the global table. The gateway additionally limits
requests to 10 per minute per connecting address, with a burst of 5.

Operators see the existing denied `auth.login` audit events and the unchanged
temporary-unavailability sign-in message; no client address or password is added
to those events. Wait for the window to expire. Clients sharing one connecting
address also share its per-client limit. If gateway DNS is unavailable or a
custom proxy is not trusted, fallback can group users under that proxy's address.

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
take effect at the next sign-in or session expiry. Cross-owner matter deletion
has the stricter [operation authorization](CASE_LIFECYCLE.md#owner-or-administrator-workflow):
forced fresh sign-in and validated `auth_time`, at most ten minutes for one matter,
exit and action, with another authorization on retry. No live directory lookup
or new credential/scope is introduced. Register the final external origin plus
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

Create the SPN/keytab for `HTTP/<exculpata-host>@<REALM>`, verify there is only
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
`X-Exculpata-Proxy-Secret`, or the existing `X-RecordBench-Authenticated-User`
and `X-RecordBench-Proxy-Secret`.
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
