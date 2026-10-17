# Installed authentication diagnostics

Local-account readers validate the file's safe path, ownership, permissions and
bounded contents on every access. Unchanged contents reuse the parsed snapshot;
an in-place edit is detected even if size and filesystem timestamps collide.
This adds at most a 1 MiB account-file read per snapshot access, without repeated
JSON or password-hash metadata parsing for unchanged accounts. Use the account
commands or administrator UI for mutations: their locked atomic replacement and
session-revision updates provide the supported concurrency and revocation
contract. Direct concurrent file edits do not provide that transaction contract.

An installed node must agree on three authentication settings: the intended
`auth` mode in `installation.json`, the explicit
`CASE_INTELLIGENCE_AUTH_MODE` in `config/exculpata.env` (or
`config/recordbench.env` on a legacy node), and the mode loaded by
the running application's identity service. Only `local`, `oidc`, and
`kerberos` are valid installation choices. A reachable login page or successful
Compose configuration alone cannot establish that agreement.

`./install doctor --root /srv/exculpata` checks a fresh node. A legacy node
keeps `/srv/recordbench`. Doctor checks the saved settings before
issuing Compose commands. Its live health acceptance then queries the running
app through `docker compose exec`, requiring the effective mode to match. The
same check applies to initial startup, resumed startup, updates, and rollback
health acceptance. A live mismatch stops acceptance immediately. Missing,
malformed, or unavailable live diagnostics remain unverified and cannot pass
health acceptance. A dry run checks saved settings only.

The diagnostic is separate from public `/health`. `/internal/auth-mode`
requires both a loopback peer and a purpose-specific HMAC derived from the
existing private session key. The app returns only its effective mode; the
installer reads no account records or provider credentials. The probe derives
the proof inside the app container, connects directly to loopback without
proxy variables or redirects, bounds its response, and emits only the
allowlisted mode. Neither key nor proof enters command arguments or installer
output. Arbitrary diagnostic output and errors are not printed. The installed
server disables forwarding-header interpretation so a gateway request cannot
become a loopback peer by supplying a header.

If saved settings disagree, restore the intended configuration before retrying.
If the running app disagrees, recreate it from that configuration and rerun
doctor. Editing an environment file does not update an already running
process. An older release without this diagnostic cannot establish live-mode
agreement: rollback may restore its services while reporting health
unconfirmed. Recover and review the older release separately; do not treat
service restoration as completed acceptance.

These checks do not establish successful user sign-in, OIDC provider admission,
Kerberos browser negotiation, or trusted browser TLS. Verify those separately
with synthetic accounts and the selected provider, as described in
[Authentication](AUTHENTICATION.md) and [Installation](INSTALL.md#acceptance).

Synthetic regressions exercise missing/empty/development/mismatched saved
modes, every installed mode against effective local/OIDC/Kerberos/preview/test
modes, unavailable or malformed diagnostic responses, protected probe output,
and IPv4/IPv6/wildcard gateway binds. These tests simulate container commands;
they do not replace acceptance on a real installation.
