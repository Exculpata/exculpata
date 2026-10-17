# Reference network egress

The reference Compose app joins only internal networks (`services`, `models`
and `app-egress`). It has no published ports. Ordinary direct outbound TCP is
blocked by that topology, including clients that ignore proxy settings. The
gateway continues reaching the app and optional Kerberos proxy through
`services`; the proxy alone bridges `app-egress` to `front`.

`HTTPS_PROXY` directs OIDC HTTPS connections to `egress-proxy:3128`. Both upper
and lower case proxy variables are fixed in Compose; HTTP and catch-all proxy
variables are empty. Exact internal service names are in `NO_PROXY`. Existing
internal HTTP clients keep their allowlists and communicate directly. The
proxy has no host ports, volumes, application environment file or secrets.

The proxy permits CONNECT only to the configured issuer's exact host and port.
It rejects all other destinations, plain HTTP forwarding, malformed authorities,
userinfo, paths, body headers and conflicting Host headers. DNS is resolved for
each connection; every answer must pass validation before any connection is
made, and the proxy dials that validated numeric address without resolving it
again. Empty issuer configuration starts in deny-all mode for local and Kerberos
sign-in. Requests, destinations and tunneled bytes are not logged. Concurrency,
header size/time, tunnel duration and transferred bytes are bounded.

This is a destination control, not TLS inspection: CONNECT can carry other
protocols or paths to the approved authority. The application still validates
the exact issuer, same-origin authorization/token/JWKS endpoints, signed tokens,
PKCE, nonce/state and groups. Discovery does **not** expand the allowlist.
Cross-origin endpoints remain unsupported, as before. Redirect following remains
disabled. TLS verification, SNI and optional custom CA handling stay in the app;
the proxy never receives a CA file or client secret.

The current candidate accepts globally routable issuer addresses only and rejects
private, loopback, link-local, multicast, unspecified, reserved and IPv6 transition
destinations, including mixed allowed/denied DNS answers. **Private-address issuer
compatibility is unresolved; this candidate is not ready for deployment until
that policy and its migration/test path have been reviewed.** Do not replace this
with a blanket private-network allowlist.

## Configuration and upgrades

Installer Compose invocations derive `OIDC_EGRESS_ISSUER` from the one selected
application env file's `CASE_INTELLIGENCE_OIDC_ISSUER`, only in `oidc` mode. Shell
overrides are discarded. This preserves fresh, legacy and explicitly selected
filenames without copying app configuration or secrets into the proxy. Changing
the saved issuer requires recreating the proxy and app together through the
installer. An existing proxy cannot observe a changed env file automatically.

For operator-managed direct Compose invocation, supply `OIDC_EGRESS_ISSUER`
with the same value as the selected application's issuer when creating the
services; otherwise the relay denies all. Merely setting an issuer in root
`.env` does not configure application authentication. Review the rendered graph
privately. Existing nodes must build the new proxy image and recreate the app
to gain isolation; a source checkout or passing static check does not apply it.
Install and update include `egress-proxy` in their explicit image-build targets.
It shares the application's pinned Python base; its incremental image layers
remain unmeasured in the existing advisory evaluation-capacity estimate until
controlled build acceptance. Model-selection build targets are unchanged.

## Named exceptions and limits

* `gateway` remains on `front` for ingress, with the existing single loopback
  HTTPS publication, and on `services`. Its normal bridge attachment has egress;
  this change does not implement a gateway outbound firewall.
* `model-stager` remains an operator-invoked `tools` service on `front`. It reads
  the source checkout and writes the model cache; it has no case-storage or
  application-secret mount. Model hub/CDN downloads are not confined by this
  OIDC proxy. Model runtime networks/offline flags remain unchanged.
* `clamav-updater` remains on `updates` and writes only the shared signature
  volume. It needs signature update egress; `clamav`, which parses evidence,
  remains internal. The updater has no uploaded-evidence mount.
* Host package/image acquisition, Docker DNS/control-plane behavior, host-root
  access and private deployment overlays are outside the proxy's boundary.

An operator may separately review a host DOCKER-USER/nftables policy for their
Docker version, IPv4/IPv6 routing, DNS and organizational identity dependencies.
No host rule is installed or changed here. The repository check does not audit
external overlays or prove a host firewall; retain private overlay review and
controlled live acceptance before treating a node as isolated.

## Repository contract and evidence

`make network-contract` runs `scripts/network-contract.py`; `make check`,
`make check-fast` and the full Quality gates application job include it. The
reviewed `config/egress-allowlist.toml` records Compose network definitions,
per-service memberships, published ports and network-escape settings, plus exact
Python networking expressions and literal URL/DNS-name occurrences. New Compose files,
implicit/default networks, host/service network modes, capabilities and changed
ports require review. Duplicate YAML keys and unparseable sources fail closed.
Source findings include file/line reasons. No whole source file is exempted.

The AST inventory follows ordinary imports, aliases, client construction and
sync/async with-bindings. It includes inert XML/provenance URLs and DNS-shaped
filenames as reviewed occurrences; these are not permission to contact hosts.
It is not whole-program analysis: reflection, generated code,
third-party networking, other languages and subprocess utilities still require
review. Editing both an allowance and its code is a review event, not proof that
the new destination is safe.

`tests/test_reference_network_boundary.py` extends the existing topology contract
with synthetic mutations and HTTPX environment checks. `tests/test_egress_proxy.py`
checks denial, address validation/rebinding and a bidirectional socket-pair smoke.
Socket pairs do not prove container routing or a fresh install. Live Compose
positive/negative isolation smoke, browser journeys and security/privacy review
remain acceptance requirements; unexecuted checks must be reported as such.

This implements the repository enforcement work tracked by
[#319](https://github.com/Exculpata/exculpata/issues/319). The separate architecture
contract in #305 should link here after coordination; its draft branch is not
modified by this change.
