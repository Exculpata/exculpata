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
direct peer. `EXCULPATA_LOCAL_LOGIN_TRUST_GATEWAY=1` (deprecated alias:
`RECORDBENCH_LOCAL_LOGIN_TRUST_GATEWAY=1`) enables resolving the exact addresses
of Compose service `gateway`. Compose sets this only on the `app` service.
`EXCULPATA_ALLOW_CONTAINER_BIND=1` keeps its bind meaning and does not enable
header trust. The `/auth/local` handler is synchronous, so DNS resolution and
password verification run in the worker thread pool. This relies on Compose's
operator-controlled DNS and private app listener; only the gateway publishes a
port. No private subnet is trusted.

Other deployments trust no peers by default. The app factory's
`local_gateway_peers` tuple takes precedence, and an empty tuple disables header
trust. Missing, duplicate or malformed headers from a trusted peer, untrusted
peers and failed gateway resolution fall back to the direct peer (`unknown` if
absent). Failed resolution, invalid trusted-peer headers and untrusted-peer
headers in container mode produce warnings, at most one per kind per five
minutes per process. Container binding without a gateway trust setting also
warns once per process. Warnings contain no addresses, header values, usernames
or tokens. Browser-supplied forwarded prefixes never select a key.

IPv4 clients use their full address. IPv4-mapped IPv6 addresses use the mapped
IPv4 address. Other IPv6 clients share a client budget within their /64, whether
the address came from the direct peer or a trusted header. Non-IP keys such as
`unknown` pass through. Clients sharing a connecting address share its budget;
failed gateway resolution or missing trust can group users under a proxy.

Local password sign-in has two admission lanes. A device cookie is only a
throttling signal, not a session or an authentication factor. Both lanes still
require the correct password and current account authorization.

- The **untrusted lane** applies to a new or cleared browser and to invalid,
  forged, wrong-user, revoked, expired or stale-revision device entries. Each app
  process allows at most 10 failed verifications per normalized username and
  client, 50 per client across usernames, and 100 per username bucket across
  clients in a rolling 15-minute window. A username bucket also permits at most
  200 failures per rolling 24 hours. Daily counters use 15-minute slots and retain
  a slot until its entire interval is older than 24 hours, so admission can be
  delayed by up to 15 additional minutes. A success clears only the completed
  failures for that client and username, preserving both username aggregates and
  the client's cross-username budget.
- The **device lane** applies to an active server-registered browser entry for
  the submitted username and current account revision. It allows 10 failures
  per rolling 15 minutes per device, per process. Thirty failures without a
  success revoke the device chain persistently. Device attempts do not consume or
  depend on any untrusted client, username or daily budget. A device success
  does not clear the untrusted aggregates. Device entries are not bound to an IP
  address or User-Agent.

Both lanes check admission before verifying a password and reserve capacity for
pending verifications. Pending reservations do not expire during verification.
Password verification runs outside the admission lock. Unknown users still take
the dummy Argon2 verification path when admitted. Throttled attempts never
verify, add failures or extend a window; verifier errors release reservations
without recording a password failure. There is no artificial request delay.
Throttled refusals redirect with `error=local_throttled` and `retry_minutes`,
rounded up from `Retry-After / 60` and clamped to 1..1440. The 303 response keeps
the integer `Retry-After` header in seconds. The sign-in page honors minutes only
with exactly one error value equal to `local_throttled` and exactly one
`retry_minutes` value of one to four ASCII digits whose integer is 1..1440.
It keeps the fixed message “Sign-in is temporarily unavailable. Wait a few
minutes and try again.” and appends “ Time remaining: about N minutes.” using
only the parsed integer. The singular sentence is “ Time remaining: about 1
minute.” Anything else uses the existing fixed message for the error code
without a “Time remaining” sentence; absent, repeated or unknown error codes
show no message.
URL text is never echoed. No other error redirect includes `retry_minutes`.
The retry interval accounts for every
blocking window; in-flight checks or subsequent failures may change
availability. Wrong-password errors are unchanged.

A successful password sign-in normally rotates the browser entry to a new random
128-bit device ID and marks its predecessor rotated in the same chain. The
`__Host-exculpata_device` cookie has Secure, HttpOnly, SameSite=Strict, Path=/,
no Domain and a 90-day Max-Age. It holds at most five entries, one per username,
newest first, for shared computers. Signing in as one user does not give another
user a device lane. Entries use a separate random device signing key, never the
session or login-challenge key. Password, role or enabled-state changes invalidate
all devices for that account through its session revision and account binding.
Display-name changes do not invalidate them. Secure cookies remain required in
local mode. If the cookie helper is used with `secure_cookie=False` in plain-HTTP
development, it skips issuing the device cookie. If cookie issuance fails after
a successful password check, including a busy or unavailable registry or a
predecessor revoked during verification, sign-in still succeeds without a new
device cookie. A completion transaction that fails with an I/O or runtime error
is retried once in full, including maintenance, releasing the pending reservation
without a failure, and issuing the device cookie. Cookie and replay state from
the rolled-back transaction are reset before retrying. A committed retry returns
the device cookie without a warning. If both completion attempts fail, a
generation-bound pending release is queued and tried immediately, then applied
before maintenance in the next successful transaction if necessary. Sign-in
succeeds without a device cookie and logs one fixed warning without identifiers.
Replay detected in a committed completion still revokes the chain and writes
the replay audit event.

A retained rotated entry has a 60-second grace window, inclusive of the last
second. It can use the device lane only while the chain is not revoked, its
successor has not been admitted, and its username, revision and account binding
still match. Admission means the entry passed the device limits on a later
request, even if that request fails its password check. Grace retries count
against the presented entry's own failed and pending checks, including its
persistent lifetime limit. They do not refresh its rotation deadline.
The registry persists the highest observed wall-clock time. A reading below
that time closes grace only when current-boot monotonic timing cannot vouch for
the rotation. Rotations within the current boot keep their monotonic 60-second
window across processes and wall-clock adjustments. Missing rotation timing,
a different boot identifier, or an elapsed monotonic window closes grace.
If the boot identifier is unavailable, a process-local identifier makes reopened
registries fail closed for grace. Active successors remain usable.

A successful grace retry issues another active, never-admitted successor in the
same chain. Successors from the same predecessor are siblings. Issuing one does
not rotate its never-admitted siblings. A successful sign-in with one sibling
deletes every other active sibling of that predecessor in the same transaction.
A failed attempt deletes no siblings. A deleted sibling is an unknown cookie and
uses the untrusted lane, without revoking the chain or writing a replay audit
event. An unused sibling that remains stored can be admitted after the
predecessor's grace window ends.
At most five never-admitted siblings are retained per predecessor; issuance
beyond that deletes the oldest by creation time, then insertion order.
Once a successor is admitted, its rotated predecessor cannot recover through
later unused successors. Registry admission and rotation each run in a single
transaction under the registry lock. Grace recovery writes no replay audit event.

Outside this rule, presenting a retained rotated entry with a valid signature
revokes its whole chain, uses the untrusted lane and writes a denied
`auth.device.replay` audit event without raw addresses or tokens. Completion
checks the rule again, so concurrent rotation cannot revive a revoked chain.
Old registries receive admission, successor-use and nullable predecessor digest
columns automatically. Rows without a predecessor digest keep the old behavior;
pre-upgrade rotated entries retain replay behavior because their successor-use
history is unknown. For rotated rows, `last_used` records the rotation time.
Pending counts, reservation deadlines and generation numbers are also added
automatically. Admission reserves a lifetime-budget slot in the same SQLite
transaction that checks failures plus pending attempts, across all processes.
Completion releases the slot and records a failure only for a failed password
check. Reservations expire 15 minutes after the first pending attempt in a
batch. Expiry conservatively counts unfinished attempts as failures and advances
the generation, so a crashed process cannot retain slots forever and a late
completion cannot count them twice. Opening another registry owner does not
reset live reservations. The existing in-process window still applies.
Registry lookup errors log a warning without cookie values and use the
untrusted lane. Password verification and its untrusted limits still apply;
a failed admission commit remains an unavailable sign-in.

Memory and persistent storage are bounded:

- At most 1,000 client records retain at most 50 username windows each. Fresh
  clients replace the least recently admitted client without pending checks.
  Existing clients cannot evict their own records by changing usernames. If all
  records have pending checks, admission waits on the existing condition until
  a check completes.
- Username aggregates use 10,000 fixed buckets selected by a keyed digest. Each
  retains at most 100 short-window failure timestamps and 97 daily counter slots,
  plus pending counters. These budgets survive client eviction and success.
  Bucket collisions can throttle earlier but cannot increase allowed guesses.
- `login-devices.sqlite3` in the identity runtime directory retains at most
  10,000 device rows containing keyed device and username digests, chain IDs,
  revision bindings, times, lifetime failure counts and state. Entries unused for
  90 days expire. Pruning removes the oldest rotated/revoked rows before active
  rows when making room. Replay detection covers retained rows. Device windows
  retain at most 10 failed or pending checks per retained device. Each username
  digest has at most 20 active chains. Before insertion, issuance keeps the
  newest 19 chains, ranking each by its newest active row's last-use time,
  creation time and insertion order. It revokes only excess chains, including
  their rotated rows, before global pruning. An older sibling cannot retire a
  chain whose newest active sibling is retained. At most five active siblings
  per chain gives a bound of 100 active entries per username. If this retires
  the issuing chain, password sign-in succeeds without a new device cookie.
  This prevents one username accumulating active entries at the expense of
  other users.

The registry and separate `identity-device.key` are owner-only files with mode
0600; symlinked paths are refused. Restarting the app container clears in-memory
throttles, including device windows, but preserves the registry, lifetime failure
counts and rotation/revocation state. Distributed guesses can still temporarily
block an untrusted browser, including a correct password; an existing valid
device entry keeps its separate lane. The gateway additionally limits requests
to 10 per minute per connecting address, with a burst of 5.

Include the registry and device key in the same stopped-runtime backup as account
and session state. Restore into a clean owner-controlled runtime directory with
the matching application version and mode 0600. A synthetic clean-restore test
checks persisted failures and rotation/replay state with the original directory
removed. Restoring an older backup also restores its older device state; use a
new device key when old browser entries must remain invalid. Rolling back to a
pre-device release uses the pre-upgrade backup and its matching reader; device
cookies then provide no lane until a supported release issues them again.

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
